#!/usr/bin/env python3
"""
将所有镜像仓库的 is_public 设置为 true

使用方式:
  1. 设置环境变量:
     export USERNAME="your_iam_username"
     export PASSWORD="your_iam_password"
     export DOMAIN="your_domain_name"
     export PROJECT="cn-east-3"
     export REGION="cn-east-3" 
  2. 运行:
     python3 swr_make_public.py
"""

import os
import sys
import json
import time
import requests
from typing import Optional


# ======================== 配置 ========================

IAM_ENDPOINT = "https://iam.myhuaweicloud.com"  # IAM 全局端点
SWR_ENDPOINT_TEMPLATE = "https://swr-api.{region}.myhuaweicloud.com"  # SWR 区域端点模板
MAX_RETRIES = 3          # 最大重试次数
RETRY_DELAY = 2          # 重试等待秒数
PAGE_SIZE = 100          # 每页获取仓库数（最大 1000，这里用 100 比较稳妥）


def get_env(key: str) -> str:
    """从环境变量读取配置，缺失时报错退出"""
    value = os.getenv(key)
    if not value:
        print(f"❌ 缺少环境变量: {key}")
        sys.exit(1)
    return value


# ======================== Token 管理 ========================

def obtain_token(username: str, password: str, domain: str, project: str) -> str:
    """
    通过用户名/密码获取 IAM 用户 Token（项目级）
    文档: https://support.huaweicloud.com/intl/zh-cn/api-iam/iam_30_0001.html
    """
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


# ======================== SWR API 封装 ========================

def swr_headers(token: str) -> dict:
    """构造通用 SWR 请求头"""
    return {
        "Content-Type": "application/json;charset=utf-8",
        "X-Auth-Token": token,
    }


def list_repos(token: str, region: str, offset: int = 0, limit: int = PAGE_SIZE) -> dict:
    """
    查询镜像仓库列表
    文档: https://support.huaweicloud.com/intl/zh-cn/api-swr/swr_02_0034.html
    """
    endpoint = SWR_ENDPOINT_TEMPLATE.format(region=region)
    url = f"{endpoint}/v2/manage/repos"
    params = {"offset": offset, "limit": limit}
    resp = requests.get(url, headers=swr_headers(token), params=params, timeout=30)

    if resp.status_code != 200:
        raise RuntimeError(f"查询仓库列表失败: {resp.status_code} {resp.text}")
    return resp.json()


def update_repo_visibility(
    token: str, region: str, namespace: str, repository: str, is_public: bool = True
) -> bool:
    """
    更新镜像仓库概要信息（设置为公开/私有）
    文档: https://support.huaweicloud.com/api-swr/swr_02_0032.html
    """
    endpoint = SWR_ENDPOINT_TEMPLATE.format(region=region)
    repo_encoded = repository.replace('/', '$')
    url = f"{endpoint}/v2/manage/namespaces/{namespace}/repos/{repo_encoded}"

    payload = {"is_public": is_public}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.patch(
                url, headers=swr_headers(token), json=payload, timeout=30
            )
            if resp.status_code == 201:
                return True
            print(f"  ⚠️  更新失败 (尝试 {attempt}/{MAX_RETRIES}): {resp.status_code} {resp.text}")
        except requests.RequestException as e:
            print(f"  ⚠️  请求异常 (尝试 {attempt}/{MAX_RETRIES}): {e}")

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)

    return False


# ======================== 主流程 ========================

def main():
    # 1. 读取配置
    username = os.environ["USERNAME"]
    password = os.environ["PASSWORD"]
    domain   = os.environ["DOMAIN"]
    project  = "cn-east-3"
    region   = project  # 默认与 project 相同

    # 2. 获取 Token
    token = obtain_token(username, password, domain, project)

    # 3. 遍历所有仓库（分页）
    offset = 0
    total_updated = 0
    total_failed = 0
    already_public = 0

    print("📋 开始扫描并更新镜像仓库...")

    while True:
        try:
            repos = list_repos(token, region, offset=offset, limit=PAGE_SIZE)
        except RuntimeError as e:
            print(f"❌ {e}")
            sys.exit(1)

        if not repos:
            break  # 无更多数据

        for repo in repos:
            name = repo.get("name", "")
            namespace = repo.get("namespace", "")
            is_public = repo.get("is_public", False)

            if not name or not namespace:
                print(f"  ⚠️  仓库信息不完整，跳过: {repo}")
                continue

            if is_public:
                print(f"  ⏭️  {namespace}/{name} 已公开，跳过")
                already_public += 1
                continue

            print(f"  🔄 正在将 {namespace}/{name} 设为公开...", end=" ")
            if update_repo_visibility(token, region, namespace, name, is_public=True):
                print("✅ 成功")
                total_updated += 1
            else:
                print("❌ 失败")
                total_failed += 1

        # 如果返回数量小于 PAGE_SIZE，说明已经是最后一页
        if len(repos) < PAGE_SIZE:
            break

        offset += PAGE_SIZE

    # 4. 输出汇总
    print("\n===== 执行完毕 =====")
    print(f"  已公开:   {already_public}（原本已公开）")
    print(f"  成功更新: {total_updated}")
    print(f"  更新失败: {total_failed}")
    print("====================")


if __name__ == "__main__":
    main()