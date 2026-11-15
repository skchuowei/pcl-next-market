#!/usr/bin/env python3
"""pcl-next 扩展市场索引生成器。

扫描 GitHub Topic 下的扩展仓库，读取每个仓库根目录的 plugin.json，
生成 market/index.json 供 pcl-next 客户端直接读取（客户端零 GitHub API 调用）。

只依赖标准库。由 .github/workflows/build-index.yml 每小时调用，也可本地手动跑：

    $env:GITHUB_TOKEN = "..."      # PowerShell
    python scripts/build_index.py --dry-run

设计约束（见 pcl-next 的 docs/plans/计划-扩展市场.md）：
  - 只收录「仓库根 plugin.json + 同一仓库的 Release 附件 .pclnx」这一种形态；
  - packageUrl 必须来自该仓库自己的 Release，tag 必须与清单 version 一致；
  - sha256 优先取 Release asset 的 digest，缺失时下载附件现场计算；
  - 校验不通过的仓库只打印日志，不写进索引（作者可查 Actions 日志排障）。
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request

TOPIC = os.environ.get("MARKET_TOPIC", "pclnext-plugin")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_PATH = os.path.join(REPO_ROOT, "market", "index.json")

MAX_PAGES = 10
PER_PAGE = 100
API = "https://api.github.com"
USER_AGENT = "pcl-next-market-bot (+https://github.com/skchuowei/pcl-next-market)"
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
VERSION_RE = re.compile(r"^[0-9]+(\.[0-9]+){0,3}([-+][0-9A-Za-z.-]+)?$")
KNOWN_EXTENSIONS = {
    "download.source",
    "launch.args",
    "external.tool",
    "theme.pack",
    "icon.pack",
    "modpack.source",
    "mixin.patch",
}


def log(message):
    print(message, flush=True)


def http_get(url, accept, timeout=30):
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if TOKEN:
        headers["Authorization"] = "Bearer " + TOKEN
    if url.startswith(API):
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(), dict(response.headers)


def api_get(path, default=None):
    try:
        body, _ = http_get(API + path, "application/vnd.github+json")
        return json.loads(body)
    except urllib.error.HTTPError as error:
        if error.code == 404 and default is not None:
            return default
        raise


def search_topic():
    repositories = []
    for page in range(1, MAX_PAGES + 1):
        data = api_get(
            f"/search/repositories?q=topic:{TOPIC}&sort=updated&order=desc"
            f"&per_page={PER_PAGE}&page={page}"
        )
        items = data.get("items") or []
        repositories.extend(items)
        total = min(int(data.get("total_count") or 0), 1000)
        if len(items) < PER_PAGE or len(repositories) >= total:
            break
    seen = {}
    for item in repositories:
        seen.setdefault(item.get("id"), item)
    return list(seen.values())


def fetch_manifest(item):
    full_name = item.get("full_name") or ""
    branch = item.get("default_branch") or "main"
    url = f"https://raw.githubusercontent.com/{full_name}/refs/heads/{branch}/plugin.json"
    try:
        body, _ = http_get(url, "application/vnd.github.raw+json")
    except urllib.error.HTTPError as error:
        return None, None, f"根目录没有 plugin.json（HTTP {error.code}）"
    try:
        return json.loads(body), url, None
    except json.JSONDecodeError as error:
        return None, None, f"plugin.json 不是合法 JSON：{error}"


def validate_manifest(manifest):
    """与客户端 ExtManifest.Parse 的规则保持一致，避免索引里出现客户端会拒绝的条目。"""
    plugin_id = (manifest.get("id") or "").strip()
    if not ID_RE.match(plugin_id):
        return "id 非法（小写 ASCII，2–64 位）"
    name = (manifest.get("name") or "").strip()
    if not name:
        name = plugin_id
    version = (manifest.get("version") or "").strip()
    if not VERSION_RE.match(version):
        return "version 不符合版本号格式"
    level = (manifest.get("level") or "L0").strip().upper()
    if level not in ("L0", "L1"):
        return "level 只能是 L0 / L1"
    description = (manifest.get("description") or "").strip()
    if not description:
        return "缺少 description"
    extensions = manifest.get("extensions")
    if not isinstance(extensions, list) or not extensions:
        return "extensions 为空：至少声明一个扩展点"
    unknown = [item for item in extensions if item not in KNOWN_EXTENSIONS]
    if unknown:
        return "未知扩展点：" + ", ".join(unknown)
    if level == "L1":
        if "mixin.patch" not in extensions:
            return "level=L1 必须声明 mixin.patch"
        entry = (manifest.get("entry") or "").strip()
        if not entry or not entry.lower().endswith(".dll"):
            return "level=L1 必须声明 entry 且指向 .dll"
    elif "mixin.patch" in extensions:
        return "声明 mixin.patch 但 level 不是 L1"
    return None


def tag_matches_version(tag, version):
    if not tag or not version:
        return False
    return tag.strip().lstrip("vV") == version.strip()


def pick_release(full_name, version):
    """返回 (release, asset, error)。优先 latest，其次前 5 个里的匹配版本。"""
    latest = api_get(f"/repos/{full_name}/releases/latest", default={})
    candidates = []
    if latest:
        candidates.append(latest)
    releases = api_get(f"/repos/{full_name}/releases?per_page=5") or []
    for release in releases:
        if release.get("id") not in {item.get("id") for item in candidates}:
            candidates.append(release)

    for release in candidates:
        tag = release.get("tag_name") or ""
        if not tag_matches_version(tag, version):
            continue
        for asset in release.get("assets") or []:
            name = (asset.get("name") or "").lower()
            if name.endswith(".pclnx"):
                return release, asset, None
        return release, None, f"Release {tag} 里没有 .pclnx 附件"
    return None, None, f"没有 tag 与 version（{version}）一致的 Release"


def asset_sha256(asset):
    digest = (asset.get("digest") or "").strip().lower()
    if digest.startswith("sha256:"):
        value = digest.split(":", 1)[1]
        if re.fullmatch(r"[0-9a-f]{64}", value):
            return value

    url = asset.get("browser_download_url")
    if not url:
        return None
    hasher = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        while True:
            chunk = response.read(1 << 16)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def manifest_updated_at(full_name, branch):
    commits = api_get(
        f"/repos/{full_name}/commits?path=plugin.json&sha={branch}&per_page=1", default=[]
    ) or []
    if not commits:
        return None
    commit = (commits[0].get("commit") or {})
    return (commit.get("committer") or {}).get("date") or (commit.get("author") or {}).get("date")


def download_count(full_name):
    total = 0
    for page in range(1, 4):
        releases = api_get(f"/repos/{full_name}/releases?per_page=100&page={page}", default=[]) or []
        for release in releases:
            for asset in release.get("assets") or []:
                total += max(0, int(asset.get("download_count") or 0))
        if len(releases) < 100:
            break
    return total


def build_entry(item):
    full_name = item.get("full_name") or ""
    owner = full_name.split("/")[0]
    branch = item.get("default_branch") or "main"
    if item.get("archived") or item.get("fork"):
        return None, "仓库已归档或为 fork，跳过"

    manifest, manifest_url, error = fetch_manifest(item)
    if error:
        return None, error

    error = validate_manifest(manifest)
    if error:
        return None, error

    plugin_id = (manifest.get("id") or "").strip()
    name = (manifest.get("name") or "").strip() or plugin_id
    version = (manifest.get("version") or "").strip()
    level = (manifest.get("level") or "L0").strip().upper()

    release, asset, error = pick_release(full_name, version)
    if error:
        return None, error

    sha256 = asset_sha256(asset)
    if not sha256:
        return None, "无法获取附件 sha256（Release asset 没有 digest 且下载失败）"

    tags = manifest.get("tags") if isinstance(manifest.get("tags"), list) else []

    return {
        "id": plugin_id,
        "name": name,
        "description": (manifest.get("description") or "").strip(),
        "author": {"displayName": owner, "githubLogin": owner},
        "repository": f"https://github.com/{full_name}",
        "manifestUrl": manifest_url,
        "level": level,
        "version": version,
        "packageUrl": asset.get("browser_download_url") or "",
        "sha256": sha256,
        "updatedAt": manifest_updated_at(full_name, branch) or "",
        "downloads": download_count(full_name),
        "tags": [item for item in tags if isinstance(item, str)],
        "group": "社区",
    }, None


def load_existing():
    if not os.path.exists(INDEX_PATH):
        return None
    try:
        with open(INDEX_PATH, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return None


def write_index(plugins, dry_run):
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    existing = load_existing()
    if existing and existing.get("plugins") == plugins:
        log("索引内容无变化，跳过写入")
        return 0

    document = {
        "version": 1,
        "name": "pcl-next 官方扩展索引",
        "group": "官方",
        "generatedAt": now.isoformat().replace("+00:00", "Z"),
        "tags": [],
        "plugins": plugins,
    }
    text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"

    if dry_run:
        log("---- dry-run 输出 ----")
        log(text)
        return 0

    os.makedirs(os.path.dirname(INDEX_PATH), exist_ok=True)
    with open(INDEX_PATH, "w", encoding="utf-8", newline="\n") as file:
        file.write(text)
    log(f"已写入 {INDEX_PATH}（{len(plugins)} 条）")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不写文件")
    args = parser.parse_args()

    if not TOKEN:
        log("警告：未提供 GITHUB_TOKEN，使用匿名配额（Search 10 次/分钟、60 次/小时），大概率被限流")

    log(f"扫描 Topic：{TOPIC}")
    try:
        repositories = search_topic()
    except urllib.error.HTTPError as error:
        log(f"Topic 搜索失败：HTTP {error.code} {error.reason}")
        return 1
    log(f"命中仓库 {len(repositories)} 个")

    entries = []
    skipped = []
    for item in repositories:
        full_name = item.get("full_name") or "?"
        try:
            entry, error = build_entry(item)
        except urllib.error.HTTPError as error:
            entry, error = None, f"HTTP {error.code} {error.reason}"
        except Exception as error:  # noqa: BLE001
            entry, error = None, f"{type(error).__name__}: {error}"

        if entry:
            entries.append(entry)
            log(f"收录 {entry['id']}（{full_name}）v{entry['version']}")
        else:
            skipped.append((full_name, error))
            log(f"跳过 {full_name}：{error}")

    # 旧条目：仓库仍存在但本次校验失败时，保留上一份索引里的记录，避免一次偶发失败让扩展开下架
    existing = load_existing() or {}
    old_by_repo = {item.get("repository"): item for item in existing.get("plugins") or []}
    kept = 0
    for full_name, _ in skipped:
        repo_url = f"https://github.com/{full_name}"
        old = old_by_repo.get(repo_url)
        if old:
            entries.append(old)
            kept += 1
    if kept:
        log(f"保留旧条目 {kept} 条（本次校验失败但此前已收录）")

    entries.sort(key=lambda item: (item.get("updatedAt") or "", item.get("id") or ""), reverse=True)
    log(f"最终收录 {len(entries)} 条，跳过 {len(skipped)} 个仓库")
    return write_index(entries, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
