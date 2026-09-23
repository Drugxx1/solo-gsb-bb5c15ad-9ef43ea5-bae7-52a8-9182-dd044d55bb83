# DICOM 批量匿名化 API

基于 **FastAPI + pydicom + Docker** 的 DICOM 影像批量匿名化服务。

上传一个包含 DICOM 文件的 ZIP，服务会：

1. 用**用户提供的稳定密钥**（stable secret）对患者身份做 HMAC-SHA256 派生，生成
   **患者级稳定伪名**（同一患者跨批次伪名相同；换密钥则全部不同）；
2. 重写 `PatientID` / `PatientName` 等身份字段，清空/删除 DICOM PS3.15 Annex E
   Basic Application Confidentiality Profile 要求处理的机构、人员、自由文本标签及
   **全部私有标签**；
3. 对 `StudyInstanceUID` / `SeriesInstanceUID` / `SOPInstanceUID` /
   `FrameOfReferenceUID` 以及**序列内任意深度的 UID 引用**使用一张全局映射表做
   一致性重写（同一原始 UID 永远映射到同一新 UID），从而**保持检查/系列/实例/
   RT 引用关系不变**；
4. （可选，默认开启）对每位患者在**可配置闭区间**（`date_shift_min`..`date_shift_max`，
   默认 -365..365 天）内派生一个稳定的日期偏移，平移全部 `DA`/`DT` 标签，
   患者出生与检查日期使用同一偏移；同一患者跨批次且范围相同时偏移一致；
5. 返回**标签变更预览**（逐标签 old → new）与**逐文件诊断**（非法 DICOM、
   引用缺失、UID 缺失等），并提供**匿名化 ZIP 下载**（扁平文件名，不保留原始
   目录结构，避免路径泄露 PHI）。

> ⚠️ 请妥善保管稳定密钥。服务不存储密钥，只在响应中返回其 SHA-256 指纹前 12 位
> 以便核对批次；密钥一旦丢失，无法将新旧伪名/UID 对应回原值。

---

## 一键启动

需要安装 Docker 与 Docker Compose（v2）。

```bash
docker compose up --build -d
```

启动后服务监听：

| 项目 | 地址 |
| --- | --- |
| API 基础地址 | <http://localhost:8000> |
| 交互式文档 (Swagger UI) | <http://localhost:8000/docs> |
| OpenAPI 描述 | <http://localhost:8000/openapi.json> |
| 健康检查 | <http://localhost:8000/health> |

端口可通过环境变量修改，例如改用 9000 端口：

```bash
DICOM_ANON_PORT=9000 docker compose up --build -d
# API: http://localhost:9000
```

停止：

```bash
docker compose down
```

不使用容器本地运行（Python 3.11）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/jobs` | 上传 ZIP（multipart），同步处理并返回完整标签变更预览与诊断 |
| `POST` | `/jobs/consistency-check` | 核对 2～5 个已完成任务的密钥指纹、患者伪名与 Study/Series/SOP UID 映射；支持差异明细分页（`level`/`limit`/`cursor`） |
| `GET` | `/jobs/{job_id}` | 获取任务结果（预览 + 诊断 + UID 映射概览） |
| `GET` | `/jobs/{job_id}/files` | 仅获取逐文件诊断与变更列表 |
| `GET` | `/jobs/{job_id}/download` | 下载匿名化后的 ZIP（全量） |
| `GET` | `/jobs/{job_id}/studies/{study_instance_uid}/download` | 按**匿名 StudyInstanceUID** 下载检查子集（递归补全 SOP 引用闭包，含跨检查引用） |
| `GET` | `/health` | 健康检查 |

### 表单参数（`POST /jobs`）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file` | file | 是 | `.zip` 压缩包（默认上限 512 MiB） |
| `secret` | string | 是 | 稳定密钥，至少 8 个字符 |
| `shift_dates` | boolean | 否 | 是否平移日期，默认 `true` |
| `date_shift_min` | integer | 否 | 日期平移闭区间下界（天），默认 `-365`；须与 `date_shift_max` **成对提供** |
| `date_shift_max` | integer | 否 | 日期平移闭区间上界（天），默认 `365`；须与 `date_shift_min` **成对提供** |

日期平移范围规则：

- 两者必须**同时给出或同时省略**；只给一个返回 `422`。
- 必须满足 **`-365 ≤ date_shift_min ≤ date_shift_max ≤ 365`**，越界或下界大于上界
  返回 `422`（非整数同样 `422`）。
- 偏移由 `HMAC-SHA256(secret, "date-shift", 患者身份, 区间)` 确定性地在闭区间内均匀
  选取，**同一患者跨批次且范围相同时偏移完全一致**；范围本身参与派生，因此换范围即
  换偏移（用于让不同处理策略批次可区分）。默认区间的派生与升级前版本逐字节兼容。
- 当 `shift_dates=false` 时范围参数会被接受但不起作用，每位患者的偏移固定为 `0`，
  `DA`/`DT` 不平移、`PatientBirthDate` 清空。
- 零宽区间可强制固定偏移，例如 `date_shift_min=date_shift_max=0` 表示“保留原日期”。

### curl 示例

```bash
# 1) 上传并创建任务（响应即标签变更预览）
curl -X POST http://localhost:8000/jobs \
  -F "file=@studies.zip;type=application/zip" \
  -F "secret=my-stable-secret-2026" \
  -F "shift_dates=true" \
  -o job.json

# 将日期平移范围限定为 ±30 天（两个字段必须成对出现）
curl -X POST http://localhost:8000/jobs \
  -F "file=@studies.zip;type=application/zip" \
  -F "secret=my-stable-secret-2026" \
  -F "shift_dates=true" \
  -F "date_shift_min=-30" \
  -F "date_shift_max=30" \
  -o job.json

JOB_ID=$(python3 -c "import json;print(json.load(open('job.json'))['id'])")

# 2) 查看逐文件诊断 / 变更预览
curl http://localhost:8000/jobs/$JOB_ID/files

# 3) 下载匿名化 ZIP（文件名为新 SOPInstanceUID，扁平结构）
curl -OJ http://localhost:8000/jobs/$JOB_ID/download
```

### 按匿名 StudyInstanceUID 下载检查子集

除全量下载外，还可以只下载某一个匿名检查，并由服务端**沿 SOP 引用自动补全**
该检查依赖的实例。子集下载**直接复用已完成任务 output 目录中的匿名 DICOM 文件，
不会重新解压或处理原始 ZIP**。

`study_instance_uid` 必须是任务结果中的**匿名后** UID，取自 `GET /jobs/{id}`
响应里的 `studies[].new_study_instance_uid`（或某文件的 `study_instance_uid`）。

闭包补全规则：

1. 以该匿名检查的**全部成功输出实例**作为初选集合；
2. 递归读取集合内匿名 DICOM **任意序列层级**的 `ReferencedSOPInstanceUID`
   （`ReferencedSOPSequence`、RT 的 `ContourImageSequence`、
   `ReferencedFrameOfReferenceSequence` 等），把**同任务内**被引用的实例补入集合，
   引用目标可以属于**其他匿名检查**（跨检查引用同样补入），重复扫描直至闭包稳定；
3. 只要存在引用目标**不在成功输出中**，或**一个匿名 SOP UID 对应多个输出文件**
   （输入 ZIP 内重复实例），接口返回 `409`，在 `detail.problems[]` 中给出
   **匿名 SOP UID 与原因**，**不输出残缺 ZIP**；
4. 匿名检查在任务成功输出中不存在时返回 `404`（任务不存在同为 `404`）。

成功时返回扁平 ZIP（文件名即匿名 `SOPInstanceUID`，与全量包内文件**逐字节相同**），
并通过响应头给出初选与补入文件数：

| 响应头 | 含义 |
| --- | --- |
| `X-Study-Initial-File-Count` | 初选文件数（该匿名检查的全部实例） |
| `X-Study-Added-File-Count` | 递归补入的文件数（含跨检查引用） |
| `X-Study-Subset-File-Count` | ZIP 总文件数（初选 + 补入） |

```bash
# 4) 取某个匿名 StudyInstanceUID（new_study_instance_uid），下载检查子集
STUDY_UID=$(python3 -c "import json;print(json.load(open('job.json'))['studies'][0]['new_study_instance_uid'])")

curl -OJ -D subset.headers \
  "http://localhost:8000/jobs/$JOB_ID/studies/$STUDY_UID/download"
grep -i '^x-study-' subset.headers
# x-study-initial-file-count: 3
# x-study-added-file-count: 1
# x-study-subset-file-count: 4
```

Python 示例（含 404/409 处理）：

```python
resp = requests.get(
    f"http://localhost:8000/jobs/{job_id}/studies/{study_uid}/download"
)
if resp.status_code == 404:
    print("任务或匿名检查不存在：", resp.json()["detail"]["code"])
elif resp.status_code == 409:
    detail = resp.json()["detail"]
    print("引用闭包无法闭合：", detail["code"])
    for p in detail["problems"]:
        # reason: not_in_output（引用目标不在成功输出中）
        #       | ambiguous_output（同一 SOP UID 对应多个输出文件）
        print(p["sop_instance_uid"], p["reason"], p.get("output_files"))
else:
    resp.raise_for_status()
    print("初选：", resp.headers["X-Study-Initial-File-Count"],
          "补入：", resp.headers["X-Study-Added-File-Count"])
    open("study-subset.zip", "wb").write(resp.content)
```

> 注意：匿名化时的悬空引用（`DANGLING_REFERENCE` 警告）会使引用闭包无法在本任务
> 输出内闭合，因此含此类引用的检查子集下载会返回 `409`；请改全量下载
> `GET /jobs/{id}/download`（行为不变），或在上传前补齐被引用实例。

错误响应（HTTP 4xx，`detail.code` 机器可读）：

| 状态码 | `detail.code` | 触发条件 |
| --- | --- | --- |
| `404` | `JOB_NOT_FOUND` | 任务 ID 不存在或已过期 |
| `404` | `STUDY_NOT_FOUND` | 该匿名 StudyInstanceUID 不在任务的成功输出中 |
| `409` | `JOB_NOT_READY` | 任务 `status != ready`（匿名化处理失败） |
| `409` | `REFERENCE_CLOSURE_INCOMPLETE` | 引用闭包无法在成功输出内闭合；`detail.problems[]` 逐项给出匿名 `sop_instance_uid`、`reason`（`not_in_output` / `ambiguous_output`）与 `output_files`（歧义时的冲突文件名） |

`409` 响应示例：

```jsonc
{
  "detail": {
    "code": "REFERENCE_CLOSURE_INCOMPLETE",
    "message": "引用闭包无法在本任务的成功输出内闭合，已拒绝输出子集 ZIP",
    "study_instance_uid": "2.25.8912…",
    "problems": [
      {"sop_instance_uid": "2.25.1613…",
       "reason": "not_in_output",          // 或 ambiguous_output
       "output_files": [],                  // ambiguous_output 时为冲突文件名列表
       "message": "引用目标不在本任务的成功输出中"}
    ]
  }
}
```

Python 示例：

```python
import requests

with open("studies.zip", "rb") as fh:
    resp = requests.post(
        "http://localhost:8000/jobs",
        files={"file": ("studies.zip", fh, "application/zip")},
        data={
            "secret": "my-stable-secret-2026",
            "shift_dates": "true",
            # 可选：自定义平移区间（须成对提供且 -365 <= min <= max <= 365）
            "date_shift_min": "-30",
            "date_shift_max": "30",
        },
    )
job = resp.json()
for f in job["files"]:
    print(f["name"], f["status"], [d["code"] for d in f["diagnostics"]])

requests.get(
    f"http://localhost:8000/jobs/{job['id']}/download"
).raise_for_status()
open("anonymized.zip", "wb").write(
    requests.get(f"http://localhost:8000/jobs/{job['id']}/download").content
)
```

---

## 跨任务一致性核对（`POST /jobs/consistency-check`）

分多个批次上传、但使用**同一把稳定密钥**时，确定性派生应保证：同一患者跨批次得到
相同伪名、同一原始 UID 跨批次映射到同一新 UID。本接口接收 **2～5 个已完成任务**
（`status=ready`），核对：

1. **secret 指纹**：所有任务必须来自同一密钥（SHA-256 前 12 位），否则直接 `409`；
2. **患者稳定伪名**：同一患者 key 的 `new_patient_id` / `new_patient_name` 是否一致、
   日期偏移是否一致（`shift_dates` 开关或平移区间不同会产生 `date_shift_mismatch`，
   各任务使用的 `date_shift_min/max` 在 `jobs[]` 中回显便于核对）；
3. **Study / Series / SOP 三级 UID 映射**：同一原始 UID 是否映射到同一新 UID，
   以及不同原始 UID 是否碰撞到同一新 UID。

请求体（JSON）：

```json
{"job_ids": ["0d03…", "7f2a…"]}
```

- 数组长度必须为 **2～5**，元素不可重复、不可为空白字符串，否则 `422`。

### 差异明细分页（`level` / `limit` / `cursor`）

当差异明细很多（例如跨大批次时 `only_in_one_job` 可能成千上万），可在请求中追加
`level` 对**单个层级**的差异明细做游标分页，响应会在顶层额外返回 `items` /
`next_cursor` / `has_more`；原有的完整计数（`*_count`、`summary`）与 `overall_status`
**始终完整给出，不受分页影响**。不传 `level` 时响应与旧版本完全一致（不出现任何
分页字段）。

请求字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `level` | string | 否 | `patient` / `study` / `series` / `sop`；不传则返回旧版完整报告 |
| `limit` | integer | 否 | 每页条数，**1～200**（越界返回 `422`）；缺省 50 |
| `cursor` | string | 否 | 上一页响应中的不透明 `next_cursor`，首页不传 |

```json
{"job_ids": ["0d03…", "7f2a…"], "level": "sop", "limit": 100}
```

响应在原报告基础上新增三个顶层字段：

| 字段 | 说明 |
| --- | --- |
| `items` | 当前层级本页的差异明细，按**固定业务键**稳定排序 |
| `next_cursor` | 下一页不透明游标；`has_more=false` 时为 `null` |
| `has_more` | 该层级是否还有后续明细 |

排序规则（固定业务键，先类型后标识，均为字典序，与比对执行顺序无关）：

1. `conflict`（患者层含 `date_shift_mismatch`）→ `uid_new_collision` → `only_in_one_job`；
2. 同类型内：患者层按 `patient_key`；UID 层按 `old_uid`，碰撞条目按 `new_uid`。

游标安全语义（不透明、HMAC 签名，默认有效期 **15 分钟**）：

- 游标载荷绑定**任务集合**（排序后的 `job_ids`）与**筛选条件**（`level`）；
- 被**篡改**或格式非法、**过期**、或复用到**其他任务集合 / 其他 level** 时返回 `400`
  （`detail.code` 分别为 `INVALID_CURSOR` / `CURSOR_EXPIRED` / `CURSOR_SCOPE_MISMATCH`），
  此时应丢弃游标、从第一页重新开始；
- 同一任务集合以不同顺序请求视为相同作用域，游标可继续使用；
- 游标不含 `limit`，可用不同页长继续翻页；服务重启后进程级签名密钥轮换，旧游标
  按篡改处理（`400`）。

> 注意：报告内嵌的 `patient_mappings.differences` 与各层 `uid_mappings[].differences`
> 仍按原口径截断（每层最多 500 条，冲突/碰撞始终完整）；**完整明细通过分页接口获取**，
> 截断时 `differences_truncated=true`，计数始终完整。

翻页示例（Python）：

```python
job_ids = [job1_id, job2_id]
cursor = None
while True:
    body = {"job_ids": job_ids, "level": "sop", "limit": 200}
    if cursor:
        body["cursor"] = cursor
    resp = requests.post(
        "http://localhost:8000/jobs/consistency-check", json=body
    )
    if resp.status_code == 400:
        # 游标篡改/过期/跨条件复用：回退到第一页
        cursor = None
        continue
    resp.raise_for_status()
    page = resp.json()
    for item in page["items"]:
        print(item["difference_type"], item.get("old_uid") or item.get("new_uid"))
    if not page["has_more"]:
        break
    cursor = page["next_cursor"]
```

curl 示例：

```bash
curl -X POST http://localhost:8000/jobs/consistency-check \
  -H "Content-Type: application/json" \
  -d '{"job_ids": ["0d03abcd1234", "7f2a9876ef01"]}'
```

Python 示例：

```python
resp = requests.post(
    "http://localhost:8000/jobs/consistency-check",
    json={"job_ids": [job1_id, job2_id, job3_id]},
)
if resp.status_code == 404:
    print("任务不存在：", resp.json()["detail"]["missing_job_ids"])
elif resp.status_code == 409:
    print("无法核对：", resp.json()["detail"]["code"])  # 未完成或指纹不一致
else:
    report = resp.json()
    print(report["overall_status"])      # consistent | inconsistent
    print(report["summary"])             # 各类冲突/覆盖差异计数
```

响应示例：

```jsonc
{
  "overall_status": "consistent",       // consistent | inconsistent
  "secret_fingerprint": "3efffbab4a80",
  "jobs": [
    {"id": "0d03…", "status": "ready", "secret_fingerprint": "3efffbab4a80",
     "shift_dates": true, "date_shift_min": -365, "date_shift_max": 365,
     "source_zip": "batch-a.zip",
     "file_count": 6, "output_file_count": 4, "error_count": 2,
     "warning_count": 1, "patient_count": 2, "study_count": 2,
     "series_count": 3, "sop_count": 4,
     "created_at": 1790120660.8, "expires_at": 1790124260.8}
  ],
  "patient_mappings": {
    "checked_keys": 2,                  // >=2 个任务共同出现、实际比对过的患者数
    "conflict_count": 0,                // 同 key 派生出不同伪名
    "date_shift_mismatch_count": 0,     // 伪名一致但日期偏移不同
    "only_in_one_job_count": 0,         // 患者仅出现在部分任务（覆盖范围差异）
    "shift_dates_consistent": true,
    "differences": [
      {"patient_key": "id:P-002",
       "difference_type": "only_in_one_job",   // conflict | date_shift_mismatch | only_in_one_job
       "values": {"0d03…": {"new_patient_id": "MII3…",
                            "new_patient_name": "ANON^MII3RH4JPC",
                            "date_shift_days": -46}},
       "present_in": ["0d03…"], "missing_in": ["7f2a…"]}
    ]
  },
  "uid_mappings": [
    {"level": "study",                  // study | series | sop
     "checked_old_uids": 2,
     "conflict_count": 0,               // 同一原始 UID -> 不同新 UID
     "only_in_one_job_count": 0,        // UID 仅出现在部分任务（覆盖范围差异）
     "new_uid_collision_count": 0,      // 不同原始 UID 碰撞到同一新 UID
     "differences_truncated": false,    // 明细过多时截断（计数始终完整）
     "differences": [
       {"level": "sop", "old_uid": "1.2.840.…",
        "difference_type": "conflict",  // conflict | only_in_one_job | uid_new_collision
        "mappings": {"0d03…": "2.25.111…", "7f2a…": "2.25.222…"},
        "present_in": ["0d03…", "7f2a…"], "missing_in": []}
     ]}
  ],
  "summary": {
    "patient_conflict": 0,
    "patient_date_shift_mismatch": 0,
    "uid_conflict": 0,
    "uid_new_collision": 0,
    "patient_only_in_one_job": 0,
    "uid_only_in_one_job": 0
  }
  // 请求带 level 时，顶层还会出现：
  // "items": [ {"level": "sop", "old_uid": "1.2.840.…",
  //             "difference_type": "only_in_one_job",
  //             "mappings": {"0d03…": "2.25.111…"},
  //             "present_in": ["0d03…"], "missing_in": ["7f2a…"]} ],
  // "next_cursor": "eyJ2Ijoi…（不透明，has_more=false 时为 null）",
  // "has_more": true
}
```

判定口径：

- `overall_status = "inconsistent"` 当且仅当存在患者伪名冲突、日期偏移不一致、
  UID 映射冲突或 UID 碰撞；仅出现在部分任务中的患者/UID 属于**批次覆盖范围差异**
  （`only_in_one_job`），不计为冲突；
- 同一密钥下确定性派生理论上不会产生 `conflict` / `uid_new_collision`，出现这类结果
  意味着派生算法版本不一致或数据被篡改，应停止合并这些批次；
- 差异明细每层最多内嵌返回 500 条（冲突/碰撞始终完整保留），超出时
  `differences_truncated=true`，但各类 `*_count` 计数仍然完整；需要完整明细时
  使用 `level` + `limit` + `cursor` 分页参数逐层获取。

错误响应（HTTP 4xx，`detail.code` 机器可读）：

| 状态码 | `detail.code` | 触发条件 |
| --- | --- | --- |
| `400` | `INVALID_CURSOR` | 分页游标格式/签名非法、被篡改，或服务重启后旧游标失效 |
| `400` | `CURSOR_EXPIRED` | 分页游标超过有效期（默认 15 分钟，需从第一页重新开始） |
| `400` | `CURSOR_SCOPE_MISMATCH` | 游标复用到其他任务集合或其他 `level` |
| `404` | `JOB_NOT_FOUND` | 任一任务 ID 不存在或已过期（`missing_job_ids` 给出缺失列表） |
| `409` | `JOBS_NOT_COMPLETED` | 存在 `status != ready` 的任务（失败/未完成，见 `not_ready_jobs`） |
| `409` | `SECRET_FINGERPRINT_MISMATCH` | 任务间密钥指纹不一致（`fingerprints` 给出各任务指纹） |
| `422` | —（Pydantic 校验） | ID 数量不在 2～5、重复、空白，`level` 非法，`limit` 不在 1～200，或请求体格式错误 |

`POST /jobs` 表单本身的 `422` 还包括：密钥过短、非 `.zip`、空上传、`shift_dates`
非布尔值、`date_shift_min/max` 非整数、超出 -365..365、或两者只提供其一 /
`min > max`。

---

## 响应结构（标签变更预览与诊断）

`POST /jobs` / `GET /jobs/{id}` 返回：

```jsonc
{
  "id": "0d03…",
  "status": "ready",                 // ready | failed
  "secret_fingerprint": "ab12…",     // 密钥 SHA-256 前 12 位，不回显密钥本身
  "shift_dates": true,
  "date_shift_min": -365,            // 本任务实际使用的日期平移闭区间
  "date_shift_max": 365,
  "file_count": 6,
  "output_file_count": 4,            // 实际写入匿名 ZIP 的文件数
  "error_count": 2,
  "warning_count": 1,
  "patients": [
    {"key": "id:P-001", "new_patient_id": "MII3RH4JPCRSHQ4D",
     "new_patient_name": "ANON^MII3RH4JPC", "date_shift_days": -46,
     "file_count": 3}
  ],
  "studies":  [ /* old/new StudyInstanceUID 映射 */ ],
  "series":   [ /* old/new SeriesInstanceUID 映射 */ ],
  "files": [
    {
      "name": "patient_1/rt/RS0001.dcm",
      "status": "warning",           // ok | warning | error | skipped
      "output_name": "2.25.9391….dcm",
      "study_instance_uid":  "2.25.…",
      "series_instance_uid": "2.25.…",
      "sop_instance_uid":    "2.25.…",
      "diagnostics": [
        {"code": "DANGLING_REFERENCE", "severity": "warning",
         "message": "ReferencedSOPSequence[0].ReferencedSOPInstanceUID 指向的 SOP 实例不在压缩包内：…（仍会被一致重写）"}
      ],
      "changes": [
        {"path": "ReferencedFrameOfReferenceSequence[0].RTReferencedStudySequence[0].ContourImageSequence[0]",
         "tag": "(0008,1155)", "keyword": "ReferencedSOPInstanceUID",
         "action": "REPLACE", "old": "1.2.840.…", "new": "2.25.…"},
        {"tag": "(0010,0020)", "keyword": "PatientID",
         "action": "REPLACE", "old": "P-001", "new": "MII3RH4JPCRSHQ4D"},
        {"tag": "(0008,0050)", "keyword": "AccessionNumber",
         "action": "EMPTY", "old": "ACC001", "new": null},
        {"tag": "(0010,1040)", "keyword": "PatientAddress",
         "action": "REMOVE", "old": "1 Secret Street", "new": null}
      ],
      "changes_truncated": false
    }
  ]
}
```

`action` 含义：`REPLACE`（确定性替换）、`EMPTY`（置空保留标签）、
`REMOVE`（整标签删除）。嵌套序列中的标签用点号路径标注，例如
`ReferencedFrameOfReferenceSequence[0].RTReferencedStudySequence[0]…`。

### 诊断码

| code | 严重级别 | 含义 |
| --- | --- | --- |
| `INVALID_DICOM` | error | 文件不是合法 DICOM（缺少 File Meta / 魔数不符） |
| `READ_ERROR` | error | 文件截断或标签编码损坏，无法解析 |
| `WRITE_ERROR` | error | 匿名化写出阶段异常 |
| `DANGLING_REFERENCE` | warning | 序列引用的 SOP 实例不在 ZIP 内（引用仍一致重写） |
| `MISSING_STUDY_UID` / `MISSING_SERIES_UID` / `MISSING_SOP_UID` | warning | 顶层 UID 缺失，已生成确定性替代 UID |
| `INVALID_DATE` | warning | DA/DT 值无法解析，已清空 |
| `PATIENT_IDENTITY_FALLBACK` | warning | 缺少 PatientID/Name，按 Study UID 或文件内容归组 |
| `DUPLICATE_SOP_INSTANCE` | warning | 多个文件共享同一 SOP UID，输出文件自动改名 |
| `DICOMDIR_SKIPPED` | info | DICOMDIR 不写入匿名包（其路径会泄露 PHI） |

出错文件不会进入匿名 ZIP；`X-Job-Error-Count` 响应头给出错误文件数。任务结果
默认保留 1 小时（可配置），到期自动清理磁盘。

---

## 匿名化策略说明

**身份字段**

- `PatientID`、`PatientName`：替换为患者级稳定伪名（Base32 编码的 HMAC 令牌 /
  `ANON^xxxxxx`）；嵌套序列中的患者标识同步替换。
- `PatientBirthDate`：开启日期平移时按该患者偏移平移，否则清空。
- 其余所有人名（VR=`PN`）一律置空。
- Annex E 列举的机构、站点、设备序列号、申请单号、自由文本/备注等标签：
  置空或删除（完整列表见 `app/tags.py`）。
- **全部私有标签**（odd group）删除。
- 写入审计标签：`PatientIdentityRemoved = YES`、
  `DeidentificationMethod = "DICOM-ANON-API; PS3.15 BASIC APPLICATION CONFIDENTIALITY PROFILE"`。

**UID 重写（关系保持）**

- 新 UID 形态为 `2.25.<128bit 整数>`（DICOM 规定的 ISO 9834 整数弧，无需注册根）。
- 映射由 `HMAC-SHA256(secret, "uid" | 原始UID)` 确定性得到；处理前先做两遍扫描：
  第一遍收集包内全部顶层与嵌套 UID，第二遍统一重写，保证无论引用出现在
  `ReferencedSOPSequence`、RT 的 `ContourImageSequence`、
  `ReferencedFrameOfReferenceSequence` 等任何深度，都与被引用实例的新 UID 一致。
- `SOPClassUID`、`TransferSyntaxUID` 等**类型性 UID 不重写**（重写会破坏文件语义）；
  文件元信息 `MediaStorageSOPInstanceUID` 与顶层 `SOPInstanceUID` 保持一致。
- 匿名 ZIP 内文件按**新 SOPInstanceUID 扁平命名**，不含原始路径。

**安全**

- 解压前校验 ZIP 条目路径（拒绝 `../` 穿越、绝对路径、符号链接/设备节点）。
- 解压总大小、条目数量设上限，防止 zip 炸弹；上传大小流式限制（超限返回 413）。
- 容器以非 root 用户运行；任务数据放在独立 Docker volume。

**局限与注意**

- 本服务实现的是 Basic Application Confidentiality Profile + 稳定 UID/伪名 +
  日期平移；**不做像素内刻录文字（burned-in PHI）的检测与擦除**。若来源设备会把
  姓名/编号刻录进图像，请先在采集端关闭或使用专用 OCR 擦除流程。
- 不重写封装像素数据传输语法，图像字节原样保留。
- 匿名化后建议在导入 PACS 前用抽样复核。

---

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DICOM_ANON_PORT`（仅 Compose） | `8000` | 宿主机映射端口 |
| `DICOM_ANON_WORK_DIR` | `/data/dicom-anon` | 任务工作目录 |
| `DICOM_ANON_MAX_UPLOAD_BYTES` | `536870912`（512 MiB） | 上传 ZIP 上限 |
| `DICOM_ANON_MAX_EXTRACTED_BYTES` | `2147483648`（2 GiB） | 解压总量上限 |
| `DICOM_ANON_MAX_EXTRACTED_FILES` | `20000` | 解压文件数上限 |
| `DICOM_ANON_JOB_TTL_SECONDS` | `3600` | 任务结果保留时长 |
| `DICOM_ANON_MIN_SECRET_LENGTH` | `8` | 稳定密钥最小长度 |
| `DICOM_ANON_CURSOR_TTL_SECONDS` | `900` | 一致性核对分页游标的有效期（秒） |

---

## 项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由（/jobs、/jobs/consistency-check、全量/检查子集下载、/health）
│   ├── anonymizer.py    # 两遍扫描引擎：UID 映射、标签重写、递归序列、诊断、ZIP 打包
│   ├── study_subset.py  # 按匿名 StudyInstanceUID 的引用闭包解析与子集 ZIP（复用匿名输出）
│   ├── consistency.py   # 跨任务一致性核对：密钥指纹/患者伪名/UID 映射比对与差异明细分页
│   ├── pagination.py    # 差异明细不透明分页游标：HMAC 签名、绑定任务集合/level、TTL
│   ├── crypto.py        # HMAC 确定性派生：伪名、UID、日期偏移
│   ├── tags.py          # Annex E 标签策略与参与一致性重写的 UID 标签集合
│   ├── jobs.py          # 任务存储、线程池处理、TTL 清理
│   ├── schemas.py       # 请求/响应模型（含一致性核对、分页与子集下载错误模型）
│   └── config.py        # 环境变量配置
├── tests/               # pytest：加密派生 / 引擎 / API 端到端 / 一致性核对 / 分页游标 / 检查子集（110 个用例）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 开发与测试

```bash
pip install -r requirements.txt
pip install pytest httpx
python -m pytest -q
```
