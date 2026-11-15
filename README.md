# pcl-next-market

pcl-next 启动器的扩展市场索引仓。`market/index.json` 由 GitHub Actions 每小时自动扫描 GitHub Topic `pclnext-plugin` 生成，客户端直接读取该文件，**不调用 GitHub API**。

## 索引地址

```
https://raw.githubusercontent.com/skchuowei/pcl-next-market/refs/heads/main/market/index.json
```

## 目录

| 路径 | 说明 |
|---|---|
| `market/index.json` | 索引本体，**自动生成，请勿手改**（除手动下架条目） |
| `scripts/build_index.py` | 索引生成脚本，仅标准库，可本地运行 |
| `.github/workflows/build-index.yml` | 每小时定时生成 + 自动提交 |
| `docs/上架说明.md` | 作者发布扩展到市场的完整步骤 |

## 工作流

```
作者仓库（打 topic + 根 plugin.json + Release 附件 .pclnx）
        ↓ 每小时扫描
Actions 跑 scripts/build_index.py：抓清单 → 校验 → 取 Release digest/sha256 → 生成 index.json
        ↓ 自动 commit
raw.githubusercontent.com/skchuowei/pcl-next-market/main/market/index.json
        ↓ 客户端读取（30 分钟 TTL + 失败回退旧缓存）
pcl-next 扩展市场页
```

Actions 挂掉或索引不可达时，客户端回退到按 Topic 实时检索（只看不装，因为兜底路径拿不到校验值）。

## 本地运行

```powershell
$env:GITHUB_TOKEN = "ghp_xxx"          # 不是必须，但没有会撞匿名配额
python scripts/build_index.py --dry-run # 只打印，不写文件
python scripts/build_index.py           # 真正写入 market/index.json
```

## 下架

索引里保留「最近一次成功收录」的条目：某次校验偶发失败不会让扩展自动下架。要主动下架，直接编辑 `market/index.json` 删除对应条目并提交——下一次生成若校验仍失败，不会重新加回；若校验成功，会重新收录。

## 校验规则（与客户端一致）

- 仓库必须有 topic `pclnext-plugin`，且根目录有 `plugin.json`；
- `id` 为小写 ASCII（`a-z0-9 . _ -`，2–64 位）、`version` 为版本号格式、`level` ∈ `L0/L1`；
- `extensions` 非空且只能取已定义的扩展点；`L1` 必须同时声明 `mixin.patch` 与 `entry`（指向 `.dll`）；
- 必须有一个 Release，其 tag 与清单 `version` 一致（允许 `v` 前缀），且含 `.pclnx` 附件；
- 附件 sha256 优先取 Release asset 的 `digest`，缺失时下载附件现场计算。

任何一条不满足都会被跳过，原因打印在 Actions 日志里。
