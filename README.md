# 器官分配与转运协调系统

Python 标准库独立项目。系统按器官类型、血型、地域、医疗匹配、紧急程度和等待时间排序候选患者，并管理提出、接受、转运、交接、植入或撤回流程。器官过期后所有继续流转操作都会被阻止，全部状态变化写入审计记录。

## 运行

```bash
python3 app.py --db organ_allocation.db
```

默认监听 `127.0.0.1:8203`，首页 `/`，健康检查 `/health`。

身份头：`X-User-Id`、`X-Role`。角色为 `viewer`、`hospital`、`coordinator`、`allocation_officer`、`auditor`；医院角色还需 `X-Hospital`。

## 主要接口

- `POST /api/donors`、`POST /api/candidates`：登记器官与候选患者（旧的单器官登记照常可用）。
- `POST /api/donor-batches`：多器官捐献批次登记。捐献者信息（`blood_type`、`hospital`、`region`、`available_at`）只填一遍，`organs` 数组中每件器官各写自己的 `organ` 和 `expires_at`（可附 `clinical_match`）。任一字段漏填、批次内器官重复或保存时限不晚于可用开始时间，返回 `400 invalid_batch` 并在 `details` 中逐项说明，整批不保存。
- `GET /api/donor-batches`、`GET /api/donor-batches/{id}`：协调台按批次查看，含 `allocated_count`、`pending_count`、`expired_count` 和每件器官的 `stage`；批次功能上线前的单器官记录作为单器官批次照常列出（`legacy_donor_id`）。
- `GET /api/donors/{id}/ranking`：按具体器官查看兼容候选排序。
- `POST /api/allocations`：提出唯一分配。同一批次（同一捐献者）的不同器官不能分给同一患者（按 `patient_name` 判定），违反返回 `409 same_recipient`。
- `POST /api/allocations/{id}/accept`、`withdraw`：医院确认或撤回。撤回或过期只影响该件器官，批次内其他器官不受牵连。
- `POST /api/allocations/{id}/transit`、`delay`：冷链转运和延误上报。
- `POST /api/allocations/{id}/handoff`、`handoff-accept`：来源医院发起、接收医院确认。
- `POST /api/allocations/{id}/implant`：确认植入。
- `GET /api/allocations/{id}/audit`、`GET /api/state`：完整审计和权限视图；协调角色的 `/api/state` 额外返回 `batches`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

血型兼容与评分是演示规则，不包含 HLA 分型、器官大小、病程、儿科差异和真实移植网络规则。医院身份使用请求头模拟，SQLite 环境适合原型，不处理跨机构身份信任、远程患者隐私协议和真实冷链设备接入。
