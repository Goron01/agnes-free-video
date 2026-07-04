#!/usr/bin/env python3
"""
Agnes Free Video HTTP 客户端（v20260606 用 curl 子进程替代 urllib）

背景：
- OpenClaw 沙箱内 Python urllib 调用 HTTPS POST 会卡死 30 秒
- 同请求 system curl 1 秒成功
- 解决：用 curl 子进程替代 urllib，保持 (http_code, body) 接口

提供：
- curl_request(method, url, headers, data, timeout) -> (http_code, body)
- download_file(url, output_path, timeout) -> bool
"""

import subprocess
import sys
from typing import Dict, Optional, Tuple, Union


def curl_request(
    url: str,
    method: str = "POST",
    headers: Optional[Dict[str, str]] = None,
    data: Optional[Union[str, bytes]] = None,
    timeout: int = 180,
) -> Tuple[int, str]:
    """用 curl 子进程发 HTTP 请求，绕开 Python urllib 在 OpenClaw 沙箱卡死

    Args:
        url: 请求 URL
        method: HTTP method (默认 POST)
        headers: dict of header name -> value
        data: request body (str or bytes)
        timeout: 超时秒数（同时传给 curl --max-time 和 subprocess 兜底）

    Returns:
        (http_code, body_str) - http_code=0 表示超时/连接错误
    """
    cmd = [
        "curl", "-s",
        "-w", "\n__HTTP_CODE__:%{http_code}",
        "-X", method,
        "--max-time", str(timeout),
    ]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    if data is not None:
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="ignore")
        cmd += ["--data", data]
    cmd.append(url)

    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
    except subprocess.TimeoutExpired:
        print(f"# [curl_request] subprocess timeout after {timeout + 5}s",
              file=sys.stderr)
        return 0, ""
    except Exception as e:
        print(f"# [curl_request] error: {type(e).__name__}: {e}", file=sys.stderr)
        return 0, ""

    body = r.stdout or ""
    http_code = 0
    if "__HTTP_CODE__:" in body:
        idx = body.rfind("__HTTP_CODE__:")
        try:
            http_code_str = body[idx + len("__HTTP_CODE__:"):].strip()
            http_code = int(http_code_str)
            body = body[:idx].rstrip()
        except (ValueError, IndexError):
            pass
    return http_code, body


def download_file(url: str, output_path: str, timeout: int = 900) -> bool:
    """用 curl 下载文件到本地路径（视频文件较大，timeout 单独给 900s）

    Returns:
        True = 成功, False = 失败（HTTP 4xx/5xx/网络错误）
    """
    cmd = [
        "curl", "-f", "-s",
        "-w", "\n__HTTP_CODE__:%{http_code}",
        "-L",  # 跟随重定向（视频 URL 一般都是 GCS 签名重定向）
        "--max-time", str(timeout),
        "-o", str(output_path),
        url,
    ]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
    except subprocess.TimeoutExpired:
        print(f"# [download_file] subprocess timeout after {timeout + 5}s",
              file=sys.stderr)
        return False
    except Exception as e:
        print(f"# [download_file] error: {type(e).__name__}: {e}",
              file=sys.stderr)
        return False

    # 解析 http code
    code = 0
    out = r.stdout or ""
    if "__HTTP_CODE__:" in out:
        idx = out.rfind("__HTTP_CODE__:")
        try:
            code = int(out[idx + len("__HTTP_CODE__:"):].strip())
        except (ValueError, IndexError):
            pass
    if 200 <= code < 300:
        return True
    print(f"# [download_file] HTTP {code} for {url}", file=sys.stderr)
    return False
