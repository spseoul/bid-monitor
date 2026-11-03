"""
한 번에 업데이트: update.zip 안의 파일을 PC 저장소에 복사하고, 같은 파일을 GitHub 에도 올린다.
  - zip 안 경로 = PC 저장소 기준 경로 (예: bidmon/page.html, build.py, bidmon/collectors/g2b.py)
  - config.yaml / .env / seen.json 은 절대 건드리지 않음
  - bidmon/page.html 은 docs/index.html 로도 올려 사이트에 바로 반영
  - bidmon/static/* 은 docs/ 에도 복사 (PWA 아이콘·manifest·sw.js)
사용: update.zip 을 이 파일 옆(또는 다운로드 폴더)에 두고 update.bat 더블클릭
"""
import base64, os, sys, zipfile, shutil, tempfile, time
from pathlib import Path
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
REPO = os.getenv("GITHUB_REPO", "").strip()
SUBDIR = os.getenv("GITHUB_SUBDIR", "").strip().strip("/")
API = "https://api.github.com"
H = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
NEVER = {"config.yaml", ".env", "seen.json", "access.json"}

def sub(p): return f"{SUBDIR}/{p}" if SUBDIR else p

def find_zip():
    cands = list(ROOT.glob("update*.zip"))
    dl = Path.home() / "Downloads"
    if dl.exists():
        cands += list(dl.glob("update*.zip"))
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)

def gh_sha(path):
    r = requests.get(f"{API}/repos/{REPO}/contents/{path}", headers=H, params={"ref": "main"}, timeout=60)
    return r.json().get("sha") if r.status_code == 200 else None

def upload(path, data: bytes, msg):
    b64 = base64.b64encode(data).decode()
    for attempt in range(4):
        body = {"message": msg, "content": b64, "branch": "main"}
        sha = gh_sha(path)
        if sha:
            body["sha"] = sha
        r = requests.put(f"{API}/repos/{REPO}/contents/{path}", headers=H, json=body, timeout=300)
        if r.status_code in (200, 201):
            print(f"  GitHub ↑ {path}"); return True
        if r.status_code not in (409, 422):
            print(f"  GitHub 실패 {path}: {r.status_code} {r.text[:120]}"); return False
        time.sleep(1.5)
    print(f"  GitHub 실패 {path}: 충돌 반복"); return False

def main():
    z = find_zip()
    if not z:
        print("update*.zip 을 찾지 못했습니다. (이 폴더 또는 다운로드 폴더)"); return 1
    print(f"업데이트 파일: {z}")
    if not TOKEN or not REPO:
        print("주의: .env 에 GITHUB_TOKEN / GITHUB_REPO 가 없어 PC 복사만 하고 GitHub 업로드는 건너뜁니다.")
    tmp = Path(tempfile.mkdtemp(prefix="bidmon_upd_"))
    with zipfile.ZipFile(z) as zf:
        zf.extractall(tmp)
    files = [p for p in tmp.rglob("*") if p.is_file()]
    # zip 이 폴더 하나로 감싸져 있으면 한 단계 벗김
    tops = {p.relative_to(tmp).parts[0] for p in files}
    base = tmp / next(iter(tops)) if len(tops) == 1 and (tmp / next(iter(tops))).is_dir() and not (tmp / next(iter(tops))).suffix else tmp
    ok = fail = 0
    for p in files:
        rel = p.relative_to(base).as_posix()
        if p.name in NEVER or rel.startswith("docs/seen"):
            print(f"  건너뜀(보호): {rel}"); continue
        dst = ROOT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
        print(f"PC 복사: {rel}")
        data = p.read_bytes()
        targets = [sub(rel)]
        if rel == "bidmon/page.html":
            targets += ["docs/index.html"] + ([sub("docs/index.html")] if SUBDIR else [])
            shutil.copy2(p, ROOT / "docs" / "index.html") if (ROOT / "docs").exists() else None
        if rel.startswith("bidmon/static/"):
            name = rel.split("/", 2)[2]
            targets += [f"docs/{name}"]
            if (ROOT / "docs").exists():
                shutil.copy2(p, ROOT / "docs" / name)
        if TOKEN and REPO:
            for t in dict.fromkeys(targets):
                if upload(t, data, f"update: {rel}"): ok += 1
                else: fail += 1
    shutil.rmtree(tmp, ignore_errors=True)
    done = z.with_name(z.stem + ".applied" + z.suffix)
    try:
        z.rename(done); print(f"적용 완료 → {done.name} 으로 이름 변경")
    except Exception:
        pass
    print(f"\n완료: GitHub 업로드 {ok}건" + (f", 실패 {fail}건" if fail else ""))
    return 0 if not fail else 2

if __name__ == "__main__":
    sys.exit(main())
