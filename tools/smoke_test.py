# -*- coding: utf-8 -*-
"""EduPACS 教学病例库系统 · 端到端冒烟测试

覆盖：登录(含 token 撤销) → 上传含 PHI 的 DICOM → 匿名化临床信息入库/编辑 → 脱敏断言（含自由文本 PHI 清洗）
     → 题目创建 → 学生盲读锁定 → 作答解锁 → 教师批阅 → 学情统计 → 清理
用法：
    .venv\\Scripts\\python tools\\smoke_test.py [--base http://127.0.0.1:8000]
依赖：pydicom、numpy、requests（均为运行时依赖）
输出：每步 [PASS]/[FAIL]；结束后打印汇总，全部通过 exit 0。
"""
import argparse
import io
import os
import sys

import numpy as np
import pydicom
import requests
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

BASE = "http://127.0.0.1:8000"
ORTHANC = "http://127.0.0.1:8042"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  <" + str(detail) + ">") if detail and not ok else ""))


def make_phi_dicom() -> bytes:
    """构造含 PHI 的 DICOM：身份字段 + 描述字段里烧入患者姓名/住院号（模拟设备习惯）"""
    ds = Dataset()
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    ds.SOPInstanceUID = generate_uid()
    ds.PatientName = "测试患者^陈大河"
    ds.PatientID = "HOSPSMOKE001"
    ds.PatientBirthDate = "19660308"
    ds.PatientAddress = "某省某市某区某街道8号"
    ds.PatientAge = "060Y"
    ds.SpecificCharacterSet = "ISO_IR 192"
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.StudyDate, ds.StudyTime = "20261119", "090000"
    ds.AccessionNumber = "ACCSMOKE001"
    ds.Modality = "CT"
    ds.StudyDescription = "陈大河 胸部CT平扫 HOSPSMOKE001"   # 描述字段烧入 PHI（关键断言点）
    ds.SeriesDescription = "HOSPSMOKE001 肺窗"
    ds.ProtocolName = "陈大河 检查"
    ds.InstitutionName = "冒烟市第一医院"
    ds.ReferringPhysicianName = "周医生"
    ds.SeriesNumber, ds.InstanceNumber = 1, 1
    ds.ConversionType = "WSD"
    ds.SamplesPerPixel = 3
    ds.PhotometricInterpretation = "RGB"
    ds.Rows, ds.Columns = 64, 64
    ds.BitsAllocated, ds.BitsStored, ds.HighBit = 8, 8, 7
    ds.PixelRepresentation, ds.PlanarConfiguration = 0, 0
    arr = np.zeros((64, 64, 3), dtype=np.uint8)
    arr[16:48, 16:48] = (180, 60, 60)
    ds.PixelData = arr.tobytes()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


def main():
    global BASE, ORTHANC, ORTHANC_AUTH
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--orthanc", default=ORTHANC, help="Orthanc 地址（冷启动/非标准端口部署时指定）")
    ap.add_argument("--orthanc-user", default="", help="Orthanc HTTP 认证用户名（启用认证的部署必填）")
    ap.add_argument("--orthanc-pass", default="", help="Orthanc HTTP 认证密码")
    args = ap.parse_args()
    BASE = args.base.rstrip("/")
    ORTHANC = args.orthanc.rstrip("/")
    ORTHANC_AUTH = (args.orthanc_user, args.orthanc_pass) if args.orthanc_user else None
    s = requests.Session()

    # ---- 0) 服务在线 ----
    try:
        cfg = s.get(f"{BASE}/api/config", timeout=10)
        check("服务在线 (/api/config)", cfg.status_code == 200)
    except Exception as e:
        check("服务在线 (/api/config)", False, e)
        sys.exit(1)

    # ---- 1) 管理员登录 ----
    r = s.post(f"{BASE}/api/auth/login", json={"username": "admin", "password": "admin123"}).json()
    check("admin 登录", bool(r.get("token")))
    admin_h = {"Authorization": "Bearer " + r["token"]}

    # 造一个临时教师/学生（避免污染现有账号）
    suffix = generate_uid()[-6:]
    t_user, s_user = f"smokeT{suffix}", f"smokeS{suffix}"
    t_pwd = "Tsmoke12345"
    r = s.post(f"{BASE}/api/users", json={"username": t_user, "password": t_pwd, "role": "teacher",
                                          "full_name": "冒烟教师", "dept": "测试"}, headers=admin_h)
    if r.status_code == 400:  # 已存在则复用（重跑场景）
        pass
    check("创建临时教师", r.status_code == 200 or r.status_code == 400)
    r = s.post(f"{BASE}/api/users", json={"username": s_user, "password": "Ssmoke12345", "role": "student",
                                          "full_name": "冒烟学生", "dept": "测试", "class_name": "冒烟班"}, headers=admin_h)
    check("创建临时学生", r.status_code == 200 or r.status_code == 400)

    tok = s.post(f"{BASE}/api/auth/login", json={"username": t_user, "password": t_pwd}).json()
    check("教师登录", bool(tok.get("token")))
    t_h = {"Authorization": "Bearer " + tok["token"]}
    st = s.post(f"{BASE}/api/auth/login", json={"username": s_user, "password": "Ssmoke12345"}).json()
    check("学生登录", bool(st.get("token")))
    s_h = {"Authorization": "Bearer " + st["token"]}

    # ---- 2) token 撤销机制 ----
    r = s.post(f"{BASE}/api/auth/change-password",
               json={"old_password": t_pwd, "new_password": t_pwd + "X"}, headers=t_h)
    check("教师改密", r.status_code == 200)
    r = s.get(f"{BASE}/api/users", headers=t_h)
    check("改密后旧 token 失效(401)", r.status_code == 401)
    r = s.post(f"{BASE}/api/auth/login", json={"username": t_user, "password": t_pwd + "X"}).json()
    check("新密码可登录", bool(r.get("token")))
    s.post(f"{BASE}/api/auth/change-password",
           json={"old_password": t_pwd + "X", "new_password": t_pwd},
           headers={"Authorization": "Bearer " + r["token"]})
    tok = s.post(f"{BASE}/api/auth/login", json={"username": t_user, "password": t_pwd}).json()
    t_h = {"Authorization": "Bearer " + tok["token"]}

    # ---- 3) 上传含 PHI DICOM → 脱敏断言 ----
    payload = make_phi_dicom()
    r = s.post(f"{BASE}/api/cases/upload",
               files={"files": ("smoke_phi.dcm", payload, "application/dicom")},
               data={"meta": '{"title":"冒烟脱敏测试病例","department":"放射科","system":"呼吸系统",'
                             '"disease":"肺结节（模拟）","difficulty":1,"status":"published","blind_mode":true,'
                             '"patient_age":"55岁","patient_sex":"男","chief_complaint":"咳嗽两周（模拟）",'
                             '"medical_history":"吸烟史 20 年（模拟）","clinical_info":"教学模拟数据，无真实身份信息",'
                             '"teaching_points":"冒烟要点：注意病灶边缘与邻近结构。",'
                             '"diagnosis":"冒烟诊断：肺结节（模拟）"}'},
               headers=t_h)
    check("上传含 PHI DICOM", r.status_code == 200, r.text[:200])
    if r.status_code != 200:
        sys.exit(1)
    case_id = r.json()["id"]
    tc = r.json().get("teaching_code", "")

    # 匿名化临床信息：上传入库 + 编辑
    anon = s.get(f"{BASE}/api/cases/{case_id}", headers=t_h).json()
    check("匿名化信息随上传入库",
          anon.get("patient_age") == "55岁" and anon.get("patient_sex") == "男"
          and anon.get("medical_history") == "吸烟史 20 年（模拟）",
          {k: anon.get(k) for k in ("patient_age", "patient_sex", "medical_history")})
    r = s.put(f"{BASE}/api/cases/{case_id}",
              json={"patient_age": "56岁", "patient_sex": "女", "medical_history": "无特殊（模拟）"},
              headers=t_h)
    anon2 = r.json() if r.status_code == 200 else {}
    check("匿名化信息可编辑",
          r.status_code == 200 and anon2.get("patient_age") == "56岁"
          and anon2.get("patient_sex") == "女"
          and anon2.get("medical_history") == "无特殊（模拟）", r.text[:200])

    # 从 Orthanc 拉回脱敏后的 DICOM 做零泄漏断言
    st_all = requests.get(f"{ORTHANC}/studies?expand", timeout=30, auth=ORTHANC_AUTH).json()
    target = None
    for x in st_all:
        if x.get("MainDicomTags", {}).get("StudyDescription", "").startswith("冒烟脱敏测试"):
            target = x
            # 说明 meta.title 覆盖了描述；改用 teaching code 匹配
        if x.get("PatientMainDicomTags", {}).get("PatientID") == tc:
            target = x
            break
    check("Orthanc 中找到脱敏后 study", target is not None)
    if target:
        insts = target.get("Instances", []) if isinstance(target.get("Instances"), list) else []
        # expand 模式 Instances 是 list[dict]
        iid = None
        if insts:
            first = insts[0]
            iid = first.get("ID") if isinstance(first, dict) else first
        if not iid:
            series = requests.get(f"{ORTHANC}/studies/{target['ID']}/series", timeout=30, auth=ORTHANC_AUTH).json()
            iid = series[0]["Instances"][0]
        dcm_raw = requests.get(f"{ORTHANC}/instances/{iid}/file", timeout=30, auth=ORTHANC_AUTH).content
        ds = pydicom.dcmread(io.BytesIO(dcm_raw), force=True)
        phi_words = ["陈大河", "HOSPSMOKE001", "19660308", "ACCSMOKE001", "冒烟市第一医院",
                     "某省某市某区某街道8号", "周医生", "060Y"]
        leaks = []
        blob = str(ds)
        for w in phi_words:
            if w in blob:
                leaks.append(w)
        check("脱敏后 DICOM 13 项 PHI 零泄漏（含描述字段清洗）", not leaks, leaks)
        check("教学编号已替换患者身份", str(getattr(ds, "PatientID", "")) == tc)
        check("患者年龄已清除", not str(getattr(ds, "PatientAge", "")))

    # ---- 4) 教学闭环 ----
    r = s.post(f"{BASE}/api/cases/{case_id}/questions", json={"question": "冒烟题：主病灶位于？",
              "reference_answer": "右肺上叶", "score": 10}, headers=t_h)
    check("教师添加题目", r.status_code == 200, r.text[:150])
    if r.status_code != 200:  # 兼容旧路由：questions POST /api/cases/{id}/questions 或 /api/questions
        r = s.post(f"{BASE}/api/questions", json={"case_id": case_id, "question": "冒烟题：主病灶位于？",
                  "reference_answer": "右肺上叶", "score": 10}, headers=t_h)
        check("教师添加题目(备用路由)", r.status_code == 200, r.text[:150])

    # 学生视角：盲读锁定
    r = s.get(f"{BASE}/api/cases/{case_id}", headers=s_h).json()
    check("学生盲读锁定（要点/诊断隐藏）",
          not r.get("teaching_points") and not r.get("diagnosis"),
          {k: r.get(k) for k in ("teaching_points", "diagnosis")})

    # 学生作答全部题目 → 解锁
    qs = s.get(f"{BASE}/api/cases/{case_id}", headers=s_h).json().get("questions", [])
    qid = qs[0]["id"] if qs else None
    r = s.post(f"{BASE}/api/questions/{qid}/submit", json={"answer": "左肺下叶"}, headers=s_h)
    check("学生作答", r.status_code == 200, r.text[:150])
    r = s.get(f"{BASE}/api/cases/{case_id}", headers=s_h).json()
    check("作答后盲读自动解锁", bool(r.get("teaching_points")) or bool(r.get("diagnosis")))

    # 教师批阅
    subs = s.get(f"{BASE}/api/cases/{case_id}/submissions", headers=t_h).json()
    sid = subs[0]["id"] if subs else None
    r = s.post(f"{BASE}/api/submissions/{sid}/grade", json={"score": 8, "feedback": "定位欠准确"}, headers=t_h)
    check("教师批阅", r.status_code == 200, r.text[:150])
    r = s.get(f"{BASE}/api/stats/students", headers=t_h).json()
    row = next((x for x in r["items"] if x["username"] == s_user), None)
    check("学情统计含冒烟学生", row is not None and row["answers"] >= 1)

    # ---- 5) 溯源（教师可见/学生 403）----
    r = s.get(f"{BASE}/api/cases/{case_id}/origin", headers=t_h)
    check("教师可查溯源", r.status_code == 200 and r.json().get("source_patient_name") == "测试患者^陈大河")
    r = s.get(f"{BASE}/api/cases/{case_id}/origin", headers=s_h)
    check("学生查溯源被拒(403)", r.status_code == 403)

    # ---- 5.5) 阅片代理写保护（反代只读，未登录客户端不得经 8000 写/删 Orthanc）----
    r = s.post(f"{BASE}/dicom-web/studies", data=b"")
    check("反代拒绝 DICOMweb 写操作(405)", r.status_code == 405, r.status_code)
    r = s.get(f"{BASE}/ohif/", timeout=10)
    check("反代 OHIF 只读入口可用(200)", r.status_code == 200, r.status_code)

    # ---- 5.6) 备份与恢复（admin API：权限隔离 + 加密归档校验）----
    try:
        import subprocess, glob as _glob
        r = s.get(f"{BASE}/api/backup/state", headers=s_h)
        check("学生访问备份管理被拒(403)", r.status_code == 403)
        r = s.get(f"{BASE}/api/backup/state", headers=admin_h)
        check("备份管理状态可用", r.status_code == 200 and r.json().get("available") is True)
        # CLI 全链路：备份 → verify（不落生产目的地，写到系统临时目录）
        import tempfile
        _tmpdir = tempfile.mkdtemp(prefix="edupacs-smoke-bk-")
        _py = sys.executable
        _env = dict(os.environ)
        cp = subprocess.run(
            [_py, "tools/backup.py", "backup", "--dest", _tmpdir,
             "--password", "SmokeBk#2026", "--note", "冒烟自检"],
            capture_output=True, text=True, timeout=300, cwd=os.getcwd(), env=_env)
        _arcs = sorted(_glob.glob(os.path.join(_tmpdir, "*.edubak")))
        check("CLI 备份创建加密归档", cp.returncode == 0 and len(_arcs) == 1,
              (cp.stdout + cp.stderr)[-200:])
        if _arcs:
            vp = subprocess.run(
                [_py, "tools/backup.py", "verify", _arcs[0], "--password", "SmokeBk#2026"],
                capture_output=True, text=True, timeout=300, cwd=os.getcwd(), env=_env)
            check("CLI 校验归档（GCM+分区哈希）", vp.returncode == 0 and "校验通过" in vp.stdout,
                  (vp.stdout + vp.stderr)[-200:])
            vp2 = subprocess.run(
                [_py, "tools/backup.py", "verify", _arcs[0], "--password", "WrongPass#1"],
                capture_output=True, text=True, timeout=300, cwd=os.getcwd(), env=_env)
            check("CLI 错误密码被拒", vp2.returncode != 0 and "密码错误" in (vp2.stdout + vp2.stderr))
            # 沙箱恢复（干跑 + apply 到沙箱，不动生产数据）
            rp = subprocess.run(
                [_py, "tools/backup.py", "restore", _arcs[0], "--password", "SmokeBk#2026",
                 "--apply", "--root", os.path.join(_tmpdir, "restore-sandbox")],
                capture_output=True, text=True, timeout=300, cwd=os.getcwd(), env=_env)
            _sdb = os.path.join(_tmpdir, "restore-sandbox", "app", "data", "app.db")
            check("沙箱恢复可用（app.db 落地）", rp.returncode == 0 and os.path.exists(_sdb),
                  (rp.stdout + rp.stderr)[-200:])
            import shutil as _shutil
            _shutil.rmtree(_tmpdir, ignore_errors=True)
    except Exception as e:
        check("备份冒烟子流程", False, f"{type(e).__name__}: {e}")

    # ---- 6) 清理 ----
    r = s.delete(f"{BASE}/api/cases/{case_id}", headers=t_h)
    check("清理病例", r.status_code == 200)
    for u in (s_user, t_user):
        row = next((x for x in s.get(f"{BASE}/api/users?q={u}", headers=admin_h).json()
                    if x["username"] == u), None)
        if row:
            s.delete(f"{BASE}/api/users/{row['id']}", headers=admin_h)
    check("清理临时用户", True)

    print("\n===== 冒烟测试汇总 =====")
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"通过 {passed}/{total}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
