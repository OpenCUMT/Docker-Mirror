#!/usr/bin/env python3
"""
华为云 SWR 批量镜像同步脚本（支持自动同步任务创建与重复检测）

功能：
1. 查询源区域镜像仓库列表
2. 查询每个仓库已有的自动同步规则，判断是否已存在指向目标区域和组织的规则
3. 若不存在，则创建自动同步任务，并触发一次手动同步
4. 若已存在，则跳过（不重复操作）

使用方式：
  1. 设置环境变量：
     export USERNAME="your_iam_username"
     export PASSWORD="your_iam_password"
     export DOMAIN="your_domain_name"
     export PROJECT="cn-east-3"           # 项目名称
     export REGION_SRC="cn-east-3"        # 源区域
     export REGION_DST="cn-north-4"       # 目标区域
     # （可选）按组织过滤
     # export NAMESPACE="my-org"

  2. 运行：
     python3 swr_sync_images.py
"""

import os
import sys
import time
import requests
from typing import List, Optional


# ======================== 配置 ========================
IAM_ENDPOINT = "https://iam.myhuaweicloud.com"
SWR_ENDPOINT_TEMPLATE = "https://swr-api.{region}.myhuaweicloud.com"
MAX_RETRIES = 3
RETRY_DELAY = 2
PAGE_SIZE = 100


def get_env(key: str) -> str:
    """读取环境变量，缺失时报错退出"""
    value = os.getenv(key)
    if not value:
        print(f"❌ 缺少环境变量: {key}")
        sys.exit(1)
    return value


# ======================== 仓库名称编码 ========================
def encode_repo_name(repository: str) -> str:
    """
    根据华为云文档要求，将仓库名称中的斜杠 '/' 替换为 '$' 进行编码
    文档说明：
    - 获取自动同步任务列表：如果您的镜像名称repository参数中有斜杠，请先替换成$再进行请求
    - 创建自动同步任务：同上
    - 手动同步镜像：同上
    """
    return repository.replace('/', '$')


# ======================== Token 管理 ========================
def obtain_token(username: str, password: str, domain: str, project: str) -> str:
    """通过用户名/密码获取 IAM 用户 Token（项目级）"""
    url = f"{IAM_ENDPOINT}/v3/auth/tokens"
    headers = {"Content-Type": "application/json;charset=utf-8"}

    payload = {
        "auth": {
            "identity": {
                "methods": ["password"],
                "password": {
                    "user": {
                        "domain": {"name": domain},
                        "name": username,
                        "password": password,
                    }
                },
            },
            "scope": {"project": {"name": project}},
        }
    }

    print(f"🔑 正在获取 IAM Token（用户: {username}，项目: {project}）...")
    resp = requests.post(url, headers=headers, json=payload, timeout=30)

    if resp.status_code != 201:
        print(f"❌ 获取 Token 失败: {resp.status_code} {resp.text}")
        sys.exit(1)

    token = resp.headers.get("X-Subject-Token")
    if not token:
        print("❌ 响应头中未找到 X-Subject-Token")
        sys.exit(1)

    print("✅ 获取 Token 成功")
    return token


# ======================== 通用工具 ========================
def swr_headers(token: str) -> dict:
    """构造通用 SWR 请求头"""
    return {
        "Content-Type": "application/json;charset=utf-8",
        "X-Auth-Token": token,
    }


# ======================== SWR API 封装 ========================
def list_repos(
    token: str, region: str, namespace: Optional[str] = None,
    offset: int = 0, limit: int = PAGE_SIZE
) -> List[dict]:
    """查询镜像仓库列表"""
    endpoint = SWR_ENDPOINT_TEMPLATE.format(region=region)
    url = f"{endpoint}/v2/manage/repos"
    params = {"offset": offset, "limit": limit}
    if namespace:
        params["namespace"] = namespace

    resp = requests.get(url, headers=swr_headers(token), params=params, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"查询仓库列表失败: {resp.status_code} {resp.text}")
    return resp.json()


def list_auto_sync_tasks(
    token: str, region: str, namespace: str, repository: str
) -> List[dict]:
    """
    获取镜像自动同步任务列表
    API: GET /v2/manage/namespaces/{namespace}/repos/{repository}/sync_repo
    文档: https://support.huaweicloud.com/api-swr/swr_02_0007.html
    """
    repo_encoded = encode_repo_name(repository)
    endpoint = SWR_ENDPOINT_TEMPLATE.format(region=region)
    url = f"{endpoint}/v2/manage/namespaces/{namespace}/repos/{repo_encoded}/sync_repo"

    resp = requests.get(url, headers=swr_headers(token), timeout=30)
    if resp.status_code != 200:
        print(f"  ⚠️  查询自动同步任务失败: {resp.status_code} {resp.text}")
        return []
    return resp.json()


def create_auto_sync_task(
    token: str, region_src: str, namespace: str, repository: str,
    target_region: str, target_namespace: str, override: bool = True
) -> bool:
    """
    创建镜像自动同步任务
    API: POST /v2/manage/namespaces/{namespace}/repos/{repository}/sync_repo
    文档: https://support.huaweicloud.com/api-swr/swr_02_0012.html
    """
    repo_encoded = encode_repo_name(repository)
    endpoint = SWR_ENDPOINT_TEMPLATE.format(region=region_src)
    url = f"{endpoint}/v2/manage/namespaces/{namespace}/repos/{repo_encoded}/sync_repo"

    payload = {
        "remoteRegionId": target_region,
        "remoteNamespace": target_namespace,
        "syncAuto": True,
        "override": override,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                url, headers=swr_headers(token), json=payload, timeout=30
            )
            if resp.status_code == 200:
                return True
            print(f"    ⚠️  创建自动同步任务失败 (尝试 {attempt}/{MAX_RETRIES}): {resp.status_code} {resp.text}")
        except requests.RequestException as e:
            print(f"    ⚠️  请求异常 (尝试 {attempt}/{MAX_RETRIES}): {e}")

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)
    return False


def delete_auto_sync_task(
    token: str, region_src: str, namespace: str, repository: str,
    target_region: str, target_namespace: str
) -> bool:
    """
    删除镜像自动同步任务
    API: DELETE /v2/manage/namespaces/{namespace}/repos/{repository}/sync_repo
    文档: https://support.huaweicloud.com/api-swr/swr_02_0013.html
    """
    repo_encoded = encode_repo_name(repository)
    endpoint = SWR_ENDPOINT_TEMPLATE.format(region=region_src)
    url = f"{endpoint}/v2/manage/namespaces/{namespace}/repos/{repo_encoded}/sync_repo"

    payload = {
        "remoteRegionId": target_region,
        "remoteNamespace": target_namespace,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.delete(
                url, headers=swr_headers(token), json=payload, timeout=30
            )
            if resp.status_code == 200:
                return True
            print(f"    ⚠️  删除自动同步任务失败 (尝试 {attempt}/{MAX_RETRIES}): {resp.status_code} {resp.text}")
        except requests.RequestException as e:
            print(f"    ⚠️  请求异常 (尝试 {attempt}/{MAX_RETRIES}): {e}")

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)
    return False


def list_tags(
    token: str, region: str, namespace: str, repository: str,
    offset: int = 0, limit: int = PAGE_SIZE
) -> List[str]:
    """查询镜像版本（tag）列表"""
    repo_encoded = encode_repo_name(repository)
    endpoint = SWR_ENDPOINT_TEMPLATE.format(region=region)
    url = f"{endpoint}/v2/manage/namespaces/{namespace}/repos/{repo_encoded}/tags"
    params = {"offset": offset, "limit": limit}

    resp = requests.get(url, headers=swr_headers(token), params=params, timeout=30)
    if resp.status_code != 200:
        print(f"  ⚠️  获取 {namespace}/{repository} 标签列表失败: {resp.status_code} {resp.text}")
        return []

    tags = []
    for item in resp.json():
        tag = item.get("Tag")
        if tag:
            tags.append(tag)
    return tags


def sync_images_manual(
    token: str, region_src: str, namespace: str, repository: str,
    image_tags: List[str], target_region: str, target_namespace: str,
    override: bool = True,
) -> bool:
    """
    手动同步镜像
    API: POST /v2/manage/namespaces/{namespace}/repos/{repository}/sync_images
    文档: https://support.huaweicloud.com/api-swr/swr_02_0014.html
    """
    repo_encoded = encode_repo_name(repository)
    endpoint = SWR_ENDPOINT_TEMPLATE.format(region=region_src)
    url = f"{endpoint}/v2/manage/namespaces/{namespace}/repos/{repo_encoded}/sync_images"

    payload = {
        "imageTag": image_tags,
        "override": override,
        "remoteNamespace": target_namespace,
        "remoteRegionId": target_region,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                url, headers=swr_headers(token), json=payload, timeout=60
            )
            if resp.status_code == 200:
                return True
            print(f"    ⚠️  手动同步失败 (尝试 {attempt}/{MAX_RETRIES}): {resp.status_code} {resp.text}")
        except requests.RequestException as e:
            print(f"    ⚠️  请求异常 (尝试 {attempt}/{MAX_RETRIES}): {e}")

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)
    return False


# ======================== 辅助函数 ========================
def has_auto_sync_for_target(
    auto_sync_tasks: List[dict], token: str, region_src: str, namespace: str, repository: str,
    target_region: str, target_namespace: str
) -> bool:
    """
    检查自动同步任务列表中是否已存在指向指定目标区域和组织的规则
    """
    for task in auto_sync_tasks:
        if (
            task.get("remoteRegionId") == target_region
            and task.get("remoteNamespace") == target_namespace
            and task.get("override") == True
        ):
            return True
        elif (
            task.get("remoteRegionId") == target_region
            and task.get("remoteNamespace") == target_namespace
            and task.get("override") == False
        ):
            print(f"    ⚠️  已存在未设置自动覆盖的自动同步任务，立即删除")
            delete_auto_sync_task(token, region_src, namespace, repository, target_region, target_namespace)
            return False
    return False


# ======================== 主流程 ========================
def main():
    # 1. 读取配置
    username = os.environ["USERNAME"]
    password = os.environ["PASSWORD"]
    domain   = os.environ["DOMAIN"]
    project  = "ap-southeast-1"
    region_src = "ap-southeast-1"
    region_dst = "cn-east-3"
    filter_namespace = os.environ["NAMESPACE"]

    # 2. 获取 Token
    token = obtain_token(username, password, domain, project)

    # 3. 遍历源区域仓库
    offset = 0
    total_repos = 0
    total_skipped = 0  # 已有自动同步规则
    total_created = 0  # 新创建自动同步任务
    total_manual_synced = 0  # 手动同步成功
    total_manual_failed = 0  # 手动同步失败

    print(f"📋 开始扫描源区域 {region_src} 的镜像仓库...")

    while True:
        try:
            repos = list_repos(token, region_src, namespace=filter_namespace, offset=offset)
        except RuntimeError as e:
            print(f"❌ {e}")
            sys.exit(1)

        if not repos:
            break

        for repo in repos:
            name = repo.get("name")
            namespace = repo.get("namespace")
            if not name or not namespace:
                continue

            total_repos += 1
            print(f"\n{'='*60}")
            print(f"  📦 处理仓库: {namespace}/{name}")
            print(f"{'='*60}")

            # 3.1 查询已有的自动同步任务
            print(f"  🔍 查询已有自动同步任务...")
            auto_sync_tasks = list_auto_sync_tasks(token, region_src, namespace, name)

            # 3.2 检查是否已存在指向目标区域和组织的自动同步规则
            if has_auto_sync_for_target(auto_sync_tasks, token, region_src, namespace, name, region_dst, namespace):
                print(f"  ⏭️  已存在指向 {region_dst}/{namespace} 的自动同步规则，跳过")
                total_skipped += 1
                continue

            # 3.3 不存在自动同步规则 → 创建自动同步任务
            print(f"  🔧 正在创建自动同步任务 → {region_dst}/{namespace} ...", end=" ")
            if create_auto_sync_task(token, region_src, namespace, name, region_dst, namespace):
                print("✅ 创建成功")
                total_created += 1
            else:
                print("❌ 创建失败")
                continue

            # 3.4 创建成功后，立即触发一次手动同步
            tags = list_tags(token, region_src, namespace, name)
            if not tags:
                print(f"    ⏭️  无镜像版本，跳过于手动同步")
                continue

            print(f"    🏷️  发现 {len(tags)} 个版本，正在触发手动同步...", end=" ")
            if sync_images_manual(token, region_src, namespace, name, tags, region_dst, namespace):
                print("✅ 手动同步已触发")
                total_manual_synced += 1
            else:
                print("❌ 手动同步触发失败")
                total_manual_failed += 1

        if len(repos) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    # 4. 输出汇总
    print(f"\n{'='*60}")
    print(f"  执行完毕")
    print(f"{'='*60}")
    print(f"  仓库总数:       {total_repos}")
    print(f"  已有规则跳过:   {total_skipped}")
    print(f"  新创建自动任务: {total_created}")
    print(f"  手动同步成功:   {total_manual_synced}")
    print(f"  手动同步失败:   {total_manual_failed}")
    print(f"{'='*60}\n")
    print("ℹ️  手动同步是异步任务，请通过查询同步任务 API 或 SWR 控制台确认同步结果。")


if __name__ == "__main__":
    main()