import os
import re
import sys
import shutil
import argparse
import tempfile
import subprocess
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
except ImportError:
    print("请先安装依赖: pip install requests tqdm")
    sys.exit(1)

try:
    from tqdm import tqdm
except Exception:
    tqdm = None

# ----- helper funcs -----
HEADERS = {"User-Agent": "m3u8-downloader/1.0"}

def fetch_text(url, session=None, **kwargs):
    s = session or requests
    resp = s.get(url, headers=HEADERS, timeout=15, **kwargs)
    resp.raise_for_status()
    return resp.text

def fetch_bytes(url, session=None, **kwargs):
    s = session or requests
    resp = s.get(url, headers=HEADERS, timeout=20, stream=True, **kwargs)
    resp.raise_for_status()
    return resp.content

def pick_variant(m3u8_text, base_url):
    """
    如果是变体 playlist(包含 #EXT-X-STREAM-INF)，选择 bandwidth 最大的 variant。
    返回 variant_url 或 None(如果不是变体)。
    """
    lines = m3u8_text.strip().splitlines()
    variant_infos = []
    for i,line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            # parse bandwidth if present
            m = re.search(r'BANDWIDTH=(\d+)', line)
            bw = int(m.group(1)) if m else 0
            # next non-empty non-comment line is URI
            for j in range(i+1, len(lines)):
                u = lines[j].strip()
                if u and not u.startswith("#"):
                    variant_infos.append((bw, urljoin(base_url, u)))
                    break
    if not variant_infos:
        return None
    # pick highest bandwidth
    variant_infos.sort(key=lambda x: x[0], reverse=True)
    return variant_infos[0][1]

def parse_segments(m3u8_text, base_url):
    """
    解析片段 URL 列表，返回 (segments_list, key_info)
    key_info = dict with {method, uri, iv_bytes or None} if AES-128 present, else None
    """
    lines = m3u8_text.strip().splitlines()
    segments = []
    key = None
    last_iv = None

    for i,line in enumerate(lines):
        line = line.strip()
        if not line or line.startswith("#") is False and not line.startswith("#EXT"):
            # plain URI line could be here but better check below
            pass
        if line.startswith("#EXT-X-KEY"):
            # parse method and uri and iv
            m_method = re.search(r'METHOD=([^,]+)', line)
            m_uri = re.search(r'URI="([^"]+)"', line)
            m_iv = re.search(r'IV=([^,]+)', line)
            method = m_method.group(1) if m_method else None
            uri = urljoin(base_url, m_uri.group(1)) if m_uri else None
            iv = None
            if m_iv:
                ivtxt = m_iv.group(1)
                # IV can be 0x... hex
                if ivtxt.startswith("0x") or ivtxt.startswith("0X"):
                    iv = bytes.fromhex(ivtxt[2:])
                else:
                    try:
                        iv = bytes.fromhex(ivtxt)
                    except Exception:
                        iv = None
            key = {"method": method, "uri": uri, "iv": iv}
        # segment lines (non-comment, non-empty)
        if line and not line.startswith("#"):
            seg_url = urljoin(base_url, line)
            segments.append(seg_url)
    return segments, key

def download_segment(idx, url, dest_path, session, retries=3, key=None):
    """
    下载单个分片，支持 AES-128 解密(如果 key 提供并 method == AES-128)。
    key: dict with method, uri (used to fetch the key bytes), iv (bytes) or None
    """
    tmp_path = dest_path + ".part"
    if os.path.exists(dest_path):
        return True, dest_path  # already exists

    for attempt in range(1, retries+1):
        try:
            r = session.get(url, headers=HEADERS, timeout=30, stream=True)
            r.raise_for_status()
            data = r.content
            # decrypt if needed
            if key and key.get("method", "").upper() == "AES-128":
                if not have_crypto:
                    raise RuntimeError("需要 pycryptodome 支持 AES 解密，但未安装。")
                # get key bytes (fetch once)
                if not key.get("_key_bytes"):
                    kb = session.get(key["uri"], headers=HEADERS, timeout=15)
                    kb.raise_for_status()
                    key["_key_bytes"] = kb.content
                iv = key.get("iv")
                if iv is None:
                    # if IV not provided, use sequence number as big-endian 16 bytes? HLS spec: IV can be segment sequence number; but complex.
                    # We'll fallback to zero IV (not ideal). User should provide IV in playlist.
                    iv = b'\x00' * 16
                cipher = AES.new(key["_key_bytes"], AES.MODE_CBC, iv=iv)
                data = cipher.decrypt(data)
                # Note: for AES-CBC we don't strip padding automatically because some streams expect exact TS frames.
            # write atomically
            with open(tmp_path, "wb") as f:
                f.write(data)
            os.replace(tmp_path, dest_path)
            return True, dest_path
        except Exception as e:
            if attempt == retries:
                return False, str(e)
            # else retry
    return False, "unknown error"

def merge_with_ffmpeg(segment_files, output_file):
    """
    用 ffmpeg 合并 - 最稳妥的方法是把文件列表写入一个 filelist.txt 然后 ffmpeg -f concat -safe 0 -i filelist -c copy out.ts/mp4
    如果输出名以 .mp4，ffmpeg 可能需要转封装(-c copy 也可能失败)，但我们先尝试 -c copy。
    """
    tmpdir = os.path.dirname(output_file) or "."
    list_file = os.path.join(tmpdir, "ff_concat_list.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for seg in segment_files:
            # ffmpeg concat requires paths like: file 'path'
            f.write(f"file '{os.path.abspath(seg)}'\n")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
           "-i", list_file, "-c", "copy", output_file]
    try:
        subprocess.check_call(cmd)
        os.remove(list_file)
        return True, None
    except subprocess.CalledProcessError as e:
        return False, str(e)

# ----- main download logic -----

def download_m3u8(url, out, threads=16, tmp=None, keep_ts=True, session=None):
    session = session or requests.Session()
    base_url = url
    text = fetch_text(url, session=session)
    # if variant playlist, pick best
    variant = pick_variant(text, base_url)
    if variant:
        print(f"检测到变体 playlist，选择 variant: {variant}")
        base_url = variant
        text = fetch_text(variant, session=session)
    # determine base for relative paths
    parsed = urlparse(base_url)
    base_for_join = base_url.rsplit("/", 1)[0] + "/"

    segments, key = parse_segments(text, base_for_join)
    if not segments:
        raise RuntimeError("未找到任何分片 (segments) —— 可能不是正确的 m3u8。")

    print(f"共发现 {len(segments)} 个分片，线程数={threads}")

    # prepare tmp dir
    tmpdir = tmp or tempfile.mkdtemp(prefix="m3u8_dl_")
    os.makedirs(tmpdir, exist_ok=True)
    seg_files = [os.path.join(tmpdir, f"{i:06d}.ts") for i in range(len(segments))]

    # Pre-fetch key if present
    if key and key.get("method", "").upper() == "AES-128":
        if not have_crypto:
            print("提醒：playlist 使用 AES-128 加密，但未安装 pycryptodome，无法解密。")
        else:
            print(f"检测到 AES-128 加密，key uri: {key.get('uri')}")

    # download with threads
    failures = []
    if tqdm:
        pbar = tqdm(total=len(segments), desc="下载", unit="seg")
    else:
        pbar = None

    with ThreadPoolExecutor(max_workers=threads) as exe:
        future_to_idx = {}
        for idx, seg_url in enumerate(segments):
            dest = seg_files[idx]
            future = exe.submit(download_segment, idx, seg_url, dest, session, 5, key)
            future_to_idx[future] = (idx, seg_url, dest)
        for fut in as_completed(future_to_idx):
            idx, seg_url, dest = future_to_idx[fut]
            ok, info = fut.result()
            if not ok:
                failures.append((idx, seg_url, info))
            if pbar:
                pbar.update(1)
    if pbar:
        pbar.close()

    if failures:
        print(f"以下 {len(failures)} 个分片下载失败(索引, url, 错误)：")
        for f in failures[:10]:
            print(f)
        raise RuntimeError("存在下载失败，停止合并。")

    # 合并
    # 首先检查 ffmpeg 是否存在
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        print("检测到 ffmpeg，使用 ffmpeg 合并为最终文件(快速且兼容性好)...")
        success, err = merge_with_ffmpeg(seg_files, out)
        if not success:
            print(f"ffmpeg 合并失败: {err}\n尝试直接拼接 .ts 文件为 {out}.ts")
            # fallthrough to concat
        else:
            print(f"合并完成 -> {out}")
            if not keep_ts:
                try:
                    shutil.rmtree(tmpdir)
                except Exception:
                    pass
            return out
    # 如果没有 ffmpeg 或 ffmpeg 合并失败，则二进制拼接
    print("使用二进制拼接所有 .ts 片段 (输出为 .ts 容器)。")
    out_ts = out if out.lower().endswith(".ts") else out + ".ts"
    with open(out_ts, "wb") as outf:
        for seg in seg_files:
            with open(seg, "rb") as f:
                shutil.copyfileobj(f, outf)
    print(f"拼接完成 -> {out_ts}")
    if not keep_ts:
        try:
            shutil.rmtree(tmpdir)
        except Exception:
            pass
    return out_ts

# ----- CLI -----
def cil(url: str, output_base: str):
    parser = argparse.ArgumentParser(description="多线程下载 M3U8 (HLS) 视频并合并")

    parser.add_argument("--keep-ts", action="store_true", help="保留临时 ts 文件")
    args = parser.parse_args()

    output = output_base + ".mp4"
    temp = output_base + "_temp"

    print("Generate the link of output_base is: {}".format(output_base))
    try:
        out = download_m3u8(url, output, threads=32, tmp=temp, keep_ts=True)
        print("完成：", out)
    except Exception as e:
        print("出错：", e)
        sys.exit(1)

from data import videos

if __name__ == "__main__":
    if len(videos) == 0:
        print("还未输入video下载资源")
    else: 
        for video in videos:
            cil(url=video["url"], output_base=video["name"])