# -*- coding: utf-8 -*-
"""生成合成教学 DICOM 样例数据（完全合成，不含真实患者信息）"""
import json
import math
from pathlib import Path

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import (
    CTImageStorage, ComputedRadiographyImageStorage as CRImageStorage, MRImageStorage,
    ExplicitVRLittleEndian, generate_uid,
)

OUT = Path(__file__).resolve().parent / "sample_dicom"
UID_ROOT = "1.2.826.0.1.3680043.10.9997.2026"
SIZE = 512
rng = np.random.default_rng(42)


def new_file_meta(sop_class_uid: str) -> FileMetaDataset:
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = sop_class_uid
    fm.MediaStorageSOPInstanceUID = generate_uid(prefix=UID_ROOT + ".1.")
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    return fm


def ellipse_mask(yy, xx, cy, cx, ry, rx):
    return ((yy - cy) / max(ry, 1e-6)) ** 2 + ((xx - cx) / max(rx, 1e-6)) ** 2 <= 1.0


def save_slice(img: np.ndarray, out_dir: Path, idx: int, *, sop_class, patient_id,
               patient_name, study_uid, study_desc, study_date, series_uid,
               series_desc, modality, series_no, sn, slice_z, thickness=5.0,
               spacing=0.7, slope=1.0, intercept=0.0, wc=60, ww=150, body_part=""):
    ds = Dataset()
    ds.file_meta = new_file_meta(sop_class)
    ds.SOPClassUID = sop_class
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.Modality = modality
    ds.SpecificCharacterSet = "ISO_IR 192"
    ds.PatientID = patient_id
    ds.PatientName = patient_name
    ds.PatientBirthDate = "19800101"
    ds.PatientSex = "O"
    ds.AccessionNumber = patient_id
    ds.StudyID = patient_id
    ds.ReferringPhysicianName = "教学系统^模拟"
    ds.StudyDate = study_date
    ds.SeriesDate = study_date
    ds.ContentDate = study_date
    ds.StudyTime = "080000"
    ds.Manufacturer = "EduPACS 合成数据生成器"
    ds.InstitutionName = "教学病例库系统（合成数据）"
    ds.StudyDescription = study_desc
    ds.SeriesDescription = series_desc
    ds.BodyPartExamined = body_part
    ds.SeriesNumber = series_no
    ds.InstanceNumber = idx + 1
    ds.ImagePositionPatient = [str(-SIZE * spacing / 2), str(-SIZE * spacing / 2), f"{slice_z:.1f}"]
    ds.ImageOrientationPatient = ["1", "0", "0", "0", "1", "0"]
    ds.SliceThickness = str(thickness)
    ds.SpacingBetweenSlices = str(thickness)
    ds.PixelSpacing = [str(spacing), str(spacing)]
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows = SIZE
    ds.Columns = SIZE
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.RescaleSlope = str(slope)
    ds.RescaleIntercept = str(intercept)
    ds.WindowCenter = str(wc)
    ds.WindowWidth = str(ww)
    ds.ImageType = ["DERIVED", "SECONDARY"]
    ds.SmallestImagePixelValue = int(img.min())
    ds.LargestImagePixelValue = int(img.max())
    ds.PixelData = img.astype(np.uint8).tobytes()
    out_dir.mkdir(parents=True, exist_ok=True)
    ds.save_as(str(out_dir / f"IM{idx + 1:04d}.dcm"), enforce_file_format=True)


# ---------------- 各部位合成图像 ----------------

def chest_ct(z_frac, nodule=False):
    """胸腔：肺野（低密度）+ 纵隔 + 脊柱 + 肋骨，HU->stored=(HU+1000)/8"""
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    img = np.full((SIZE, SIZE), 1, np.float64)  # 空气 HU -1000
    cx, cy = 256, 260
    taper = math.sin(max(z_frac, 0.06) * math.pi)  # 边缘层面变小
    body = ellipse_mask(yy, xx, cy, cx, 170 * taper, 235 * taper)
    img[body] = 130  # 软组织 ~HU 40
    # 双肺
    for sx in (-1, 1):
        lung = ellipse_mask(yy, xx, cy + 10, cx + sx * 95, 115 * taper, 75 * taper)
        img[lung] = np.clip(rng.normal(45, 6, img.shape), 5, 90)[lung]  # ~HU -640
        # 肺纹理
        for k in range(6):
            x0 = cx + sx * (40 + 9 * k)
            vein = ellipse_mask(yy, xx, cy + 10 + k * 12, x0, 6, 3)
            img[vein & lung] = 55
    # 纵隔
    med = ellipse_mask(yy, xx, cy + 5, cx, 75 * taper, 42 * taper)
    img[med] = rng.normal(132, 4, img.shape)[med]
    heart = ellipse_mask(yy, xx, cy + 55, cx + 30, 48, 38)
    img[heart] = 138
    aorta = ellipse_mask(yy, xx, cy - 25, cx + 8, 11, 11)
    img[aorta] = 142
    # 脊柱
    spine = ellipse_mask(yy, xx, cy - 130 * taper, cx, 26, 30)
    img[spine] = rng.normal(150, 6, img.shape)[spine]
    canal = ellipse_mask(yy, xx, cy - 130 * taper, cx, 10, 10)
    img[canal] = 95
    # 肋骨（随层面旋转的弧线）
    for ang in np.linspace(0.35, 2.8, 8):
        rib_cx = cx + 205 * taper * math.cos(ang)
        rib_cy = cy + 150 * taper * math.sin(ang)
        rib = ellipse_mask(yy, xx, rib_cy, rib_cx, 7, 14)
        img[rib & body] = 225  # 骨 ~HU 800
    if nodule:
        nod = ellipse_mask(yy, xx, cy - 20, cx + 105, 13, 13)
        img[nod] = 152  # ~HU 216 高密度结节
    img = np.clip(img + rng.normal(0, 3, img.shape), 0, 255)
    return img.astype(np.uint8)


def brain_mr(z_frac, lesion=False):
    """颅脑 MR：颅骨环 + 脑实质纹理 + 脑室，可选占位"""
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    cx, cy = 256, 256
    taper = math.sin(max(z_frac, 0.05) * math.pi)
    r_out = 208 * taper
    img = np.zeros((SIZE, SIZE), np.float64)
    head = ellipse_mask(yy, xx, cy, cx, r_out, r_out * 0.92)
    img[head] = rng.normal(58, 9, img.shape)[head]  # 脑实质
    scalp = ellipse_mask(yy, xx, cy, cx, r_out, r_out * 0.92) & ~ellipse_mask(yy, xx, cy, cx, r_out - 9, (r_out - 9) * 0.92)
    img[scalp] = 185  # 头皮下脂肪亮
    inner = ellipse_mask(yy, xx, cy, cx, r_out - 11, (r_out - 11) * 0.92) & ~ellipse_mask(yy, xx, cy, cx, r_out - 20, (r_out - 20) * 0.92)
    img[inner] = rng.normal(38, 5, img.shape)[inner]  # 颅骨内板低信号
    # 脑室
    for sx in (-1, 1):
        vent = ellipse_mask(yy, xx, cy - 25, cx + sx * 28, 38, 13)
        img[vent] = 22
    # 脑沟纹理
    grooves = (rng.random(img.shape) > 0.994) & ellipse_mask(yy, xx, cy, cx, r_out - 22, (r_out - 22) * 0.92)
    img[grooves] = 30
    if lesion:
        les = ellipse_mask(yy, xx, cy - 60, cx + 85, 34, 30)
        img[les] = rng.normal(118, 7, img.shape)[les]  # 高信号占位
        ring = ellipse_mask(yy, xx, cy - 60, cx + 85, 40, 36) & ~les
        img[ring] = 70  # 水肿带
    img = np.clip(img + rng.normal(0, 4, img.shape), 0, 255)
    return img.astype(np.uint8)


def abdomen_ct(z_frac, lesion=False):
    """腹部：肝 + 脾 + 肾 + 脊柱"""
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    cx, cy = 256, 266
    taper = math.sin(max(z_frac, 0.08) * math.pi)
    img = np.full((SIZE, SIZE), 4, np.float64)
    body = ellipse_mask(yy, xx, cy, cx, 165 * taper, 240 * taper)
    img[body] = 128
    liver = ellipse_mask(yy, xx, cy - 35, cx - 95, 95 * taper, 105 * taper)
    img[liver] = rng.normal(120, 6, img.shape)[liver]  # 肝实质
    stomach = ellipse_mask(yy, xx, cy - 45, cx + 95, 55 * taper, 55 * taper)
    img[stomach] = rng.normal(95, 8, img.shape)[stomach]
    spleen = ellipse_mask(yy, xx, cy - 20, cx + 180, 38, 30)
    img[spleen] = 118
    for sx in (-1, 1):
        kidney = ellipse_mask(yy, xx, cy + 60, cx + sx * 105, 34, 24)
        img[kidney] = 125
        pelvis = ellipse_mask(yy, xx, cy + 60, cx + sx * 105, 9, 8)
        img[pelvis] = 70
    spine = ellipse_mask(yy, xx, cy - 120 * taper, cx, 28, 33)
    img[spine] = rng.normal(158, 6, img.shape)[spine]
    aorta = ellipse_mask(yy, xx, cy - 45, cx - 30, 12, 12)
    img[aorta] = 148
    if lesion:
        les = ellipse_mask(yy, xx, cy - 55, cx - 120, 22, 22)
        img[les] = rng.normal(65, 6, img.shape)[les]  # 低密度肝占位
    img = np.clip(img + rng.normal(0, 3, img.shape), 0, 255)
    return img.astype(np.uint8)


def knee_cr(view):
    """膝关节 CR：股骨远端 + 胫骨近端 + 髌骨/关节间隙"""
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    cx, cy = 256, 256
    img = np.full((SIZE, SIZE), 55, np.float64)  # 软组织
    femur = ellipse_mask(yy, xx, cy - 130, cx, 150, 85) & (yy < cy - 40)
    img[femur] = 205
    femur_med = ellipse_mask(yy, xx, cy - 40, cx, 70, 92)
    img[femur_med] = 195
    tibia = ellipse_mask(yy, xx, cy + 150, cx, 150, 78) & (yy > cy + 45)
    img[tibia] = 200
    tibia_head = ellipse_mask(yy, xx, cy + 45, cx, 62, 85)
    img[tibia_head] = 190
    fibula = ellipse_mask(yy, xx, cy + 120, cx + 95, 130, 26)
    img[fibula] = 185
    gap = ellipse_mask(yy, xx, cy + 4, cx, 14, 88)
    img[gap] = 60  # 关节间隙
    img[(yy > cy - 40) & (yy < cy - 30)] = 165
    if view == "lateral":
        patella = ellipse_mask(yy, xx, cy - 95, cx + 110, 55, 26)
        img[patella] = 210
    img = np.clip(img + rng.normal(0, 5, img.shape), 0, 255)
    return img.astype(np.uint8)


def make_study(name, patient_id, patient_name, study_desc, modality, series_desc,
               builder, n_slices, body_part, wc, ww, slope, intercept,
               variant=False, sop_class=CTImageStorage, date="20261101"):
    study_uid = generate_uid(prefix=UID_ROOT + ".2.")
    series_uid = generate_uid(prefix=UID_ROOT + ".3.")
    out_dir = OUT / name
    for i in range(n_slices):
        z = i / max(n_slices - 1, 1)
        img = builder(z, variant) if n_slices > 1 else builder(variant)
        save_slice(img, out_dir, i, sop_class=sop_class, patient_id=patient_id,
                   patient_name=patient_name, study_uid=study_uid, study_desc=study_desc,
                   study_date=date, series_uid=series_uid, series_desc=series_desc,
                   modality=modality, series_no=1, sn=i, slice_z=(z - 0.5) * n_slices * 5.0,
                   body_part=body_part, wc=wc, ww=ww, slope=slope, intercept=intercept)
    return {"study_uid": study_uid, "series_uid": series_uid, "dir": name}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = []
    studies = [
        dict(name="CT_CHEST_NORMAL", patient_id="JX001", patient_name="教学001^正常胸",
             study_desc="胸部CT平扫（正常对照）", modality="CT", series_desc="Axial 5.0mm",
             builder=chest_ct, n=12, body="CHEST", wc=-600, ww=1500, slope=8, intercept=-1000, variant=False,
             meta=dict(title="胸部CT平扫（正常对照）", department="放射科", system="呼吸系统",
                       disease="正常胸部解剖", difficulty=1, tags="CT,正常解剖,肺窗",
                       teaching_points="辨认双肺野、肺纹理、纵隔大血管、心脏、脊柱与肋骨的正常CT解剖；练习肺窗与纵隔窗切换。",
                       diagnosis="胸部CT平扫未见明确异常。", modality="CT")),
        dict(name="CT_CHEST_NODULE", patient_id="JX002", patient_name="教学002^肺结节",
             study_desc="胸部CT平扫（肺内结节）", modality="CT", series_desc="Axial 5.0mm",
             builder=chest_ct, n=12, body="CHEST", wc=-600, ww=1500, slope=8, intercept=-1000, variant=True,
             meta=dict(title="胸部CT平扫（肺内结节）", department="放射科", system="呼吸系统",
                       disease="肺结节（模拟）", difficulty=2, tags="CT,肺结节,阅片训练",
                       teaching_points="在肺窗中寻找右肺高密度结节灶，描述其位置、大小、边缘特征；对比正常对照病例。",
                       diagnosis="右肺上叶可见类圆形高密度结节灶（模拟病灶），建议随访复查。", modality="CT")),
        dict(name="MR_BRAIN_NORMAL", patient_id="JX003", patient_name="教学003^正常脑",
             study_desc="颅脑MR平扫（正常对照）", modality="MR", series_desc="Axial T1WI",
             builder=brain_mr, n=10, body="BRAIN", wc=60, ww=150, slope=1, intercept=0, variant=False,
             meta=dict(title="颅脑MR平扫（正常对照）", department="放射科", system="神经系统",
                       disease="正常颅脑解剖", difficulty=1, tags="MR,正常解剖,T1WI",
                       teaching_points="辨认脑实质灰白质分界、脑室系统、中线结构；理解MR多序列成像特点。",
                       diagnosis="颅脑MRI平扫未见明确异常。", modality="MR")),
        dict(name="MR_BRAIN_LESION", patient_id="JX004", patient_name="教学004^脑占位",
             study_desc="颅脑MR平扫（占位病变）", modality="MR", series_desc="Axial T1WI",
             builder=brain_mr, n=10, body="BRAIN", wc=60, ww=150, slope=1, intercept=0, variant=True,
             meta=dict(title="颅脑MR平扫（占位病变）", department="放射科", system="神经系统",
                       disease="颅内占位（模拟）", difficulty=3, tags="MR,占位,阅片训练",
                       teaching_points="寻找左侧额顶叶异常信号灶，描述其位置、大小、周围水肿带与中线移位情况。",
                       diagnosis="左侧额顶叶可见类圆形异常信号灶伴周围水肿（模拟病灶）。", modality="MR")),
        dict(name="CT_ABDOMEN_LIVER", patient_id="JX005", patient_name="教学005^肝占位",
             study_desc="腹部CT平扫（肝内低密度灶）", modality="CT", series_desc="Axial 5.0mm",
             builder=abdomen_ct, n=10, body="ABDOMEN", wc=50, ww=350, slope=8, intercept=-1000, variant=True,
             meta=dict(title="腹部CT平扫（肝内低密度灶）", department="放射科", system="消化系统",
                       disease="肝占位（模拟）", difficulty=2, tags="CT,腹部,肝占位",
                       teaching_points="辨认肝、脾、双肾、胰腺层面结构；寻找肝右叶低密度灶并测量CT值。",
                       diagnosis="肝右叶可见类圆形低密度灶（模拟病灶）。", modality="CT")),
        dict(name="CR_KNEE", patient_id="JX006", patient_name="教学006^膝关节",
             study_desc="膝关节DR正侧位（正常对照）", modality="CR", series_desc="正侧位",
             builder=None, n=2, body="KNEE", wc=140, ww=380, slope=1, intercept=0,
             meta=dict(title="膝关节DR正侧位（正常对照）", department="放射科", system="运动系统",
                       disease="正常膝关节X线解剖", difficulty=1, tags="CR,X线,骨关节",
                       teaching_points="辨认股骨远端、胫骨平台、髌骨、腓骨头与关节间隙；理解X线正侧位对应关系。",
                       diagnosis="膝关节正侧位未见明确骨折及脱位征象。", modality="CR")),
    ]
    for s in studies:
        if s["modality"] == "CR":
            study_uid = generate_uid(prefix=UID_ROOT + ".2.")
            for i, view in enumerate(["PA", "lateral"]):
                img = knee_cr(view)
                save_slice(img, OUT / s["name"], i, sop_class=CRImageStorage,
                           patient_id=s["patient_id"], patient_name=s["patient_name"],
                           study_uid=study_uid, study_desc=s["study_desc"],
                           study_date="20261101",
                           series_uid=generate_uid(prefix=UID_ROOT + ".3."),
                           series_desc="PA位" if view == "PA" else "侧位",
                           modality="CR", series_no=1 + i, sn=i, slice_z=0,
                           body_part=s["body"], wc=s["wc"], ww=s["ww"],
                           slope=s["slope"], intercept=s["intercept"])
            info = {"study_uid": study_uid}
        else:
            sop = CTImageStorage if s["modality"] == "CT" else MRImageStorage
            info = make_study(s["name"], s["patient_id"], s["patient_name"], s["study_desc"],
                              s["modality"], s["series_desc"], s["builder"], s["n"],
                              s["body"], s["wc"], s["ww"], s["slope"], s["intercept"],
                              variant=s["variant"], sop_class=sop)
        m = s["meta"].copy()
        m["sample_dir"] = s["name"]
        m["study_uid"] = info["study_uid"]
        manifest.append(m)
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(1 for p in OUT.rglob("*.dcm"))
    print(f"生成完成：{len(manifest)} 个研究 / {total} 个 DICOM 文件，目录 {OUT}")


if __name__ == "__main__":
    main()
