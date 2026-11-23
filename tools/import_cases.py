# -*- coding: utf-8 -*-
"""样例数据一键导入：登录 -> 上传病例 -> 配题 -> 发布 -> 创建演示账号"""
import io
import json
import sys
import zipfile
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8000"
SAMPLE = Path(__file__).resolve().parent / "sample_dicom"


def login(username="admin", password="admin123"):
    r = requests.post(f"{BASE}/api/auth/login", json={"username": username, "password": password}, timeout=30)
    r.raise_for_status()
    return r.json()["token"]


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def upload_case(token, dcm_files, meta):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in dcm_files:
            zf.write(p, arcname=p.name)
    buf.seek(0)
    r = requests.post(
        f"{BASE}/api/cases/upload",
        headers=headers(token),
        data={"meta": json.dumps(meta, ensure_ascii=False)},
        files={"files": ("sample_study.zip", buf, "application/zip")},
        timeout=600,
    )
    if r.status_code != 200:
        print("上传失败:", r.status_code, r.text[:300])
        return None
    return r.json()


def main():
    manifest = json.loads((SAMPLE / "manifest.json").read_text(encoding="utf-8"))
    token = login()
    print("登录成功 admin")

    # 演示账号：1 名教师 + 100 名学生
    users_csv = ["teacher01,teach123,teacher,李老师,医学影像教研室,影像2401班"]
    for i in range(1, 101):
        users_csv.append(f"st{i:03d},st{i:03d}@pass,student,学生{i:03d},医学影像系,影像2401班")
    r = requests.post(f"{BASE}/api/users/import", headers=headers(token),
                      data={"content": "\n".join(users_csv)}, timeout=120)
    print("演示账号导入:", r.json())

    for m in manifest:
        dcm_files = sorted((SAMPLE / m["sample_dir"]).glob("*.dcm"))
        meta = {k: v for k, v in m.items() if k not in ("sample_dir", "study_uid")}
        meta["status"] = "published"
        res = upload_case(token, dcm_files, meta)
        if not res:
            continue
        case_id = res["id"]
        q_default = [
            {"question": f"请描述本病例的主要影像学表现（部位、大小、密度/信号、边界）。",
             "reference_answer": m.get("teaching_points", ""), "score": 10},
            {"question": "请给出初步影像诊断与鉴别诊断思路。",
             "reference_answer": m.get("diagnosis", ""), "score": 10},
        ]
        for i, q in enumerate(q_default):
            requests.post(f"{BASE}/api/cases/{case_id}/questions", headers=headers(token),
                          json=q | {"sort_no": i}, timeout=30)
        print(f"病例 #{case_id} 上传成功：{m['title']}（{res['uploaded']} 文件 / {res['image_count']} 幅图像）")

    print("全部导入完成")


if __name__ == "__main__":
    sys.exit(main())
