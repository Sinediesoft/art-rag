"""建置生產排程服務（make scheduler-setup）：Timefold Solver（Java 21）＋Maven。

1. 找 Java 21 以上：JAVA_HOME → Homebrew 的 openjdk@21 → /usr/libexec/java_home → PATH 上的 java
   （macOS：brew install openjdk@21；Ubuntu／WSL2：sudo apt install openjdk-21-jdk-headless）
2. 找 Maven：PATH 上的 mvn，沒有就從 Maven Central 下載到 scheduler/.maven/
   （約 9 MB，驗證 SHA-512）
   ——不用 brew install maven，因為它會再裝一套最新版 JDK
3. 在 scheduler/ 執行 mvn package（含單元測試），產生 scheduler/target/scheduler.jar；
   找到的 JAVA_HOME 寫進 scheduler/.java-home，給 make scheduler 使用

用法：
    python pipelines/setup_scheduler.py               # 建置（第一次會下載 Timefold 等套件約 30 MB）
    python pipelines/setup_scheduler.py --skip-tests
"""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SCHED = ROOT / "scheduler"
MAVEN_VERSION = "3.9.16"
MAVEN_URL = (
    "https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/"
    f"{MAVEN_VERSION}/apache-maven-{MAVEN_VERSION}-bin.tar.gz"
)
MIN_JAVA = 21
# Windows 的執行檔是 java.exe、mvn.cmd（bin/mvn 是 sh 腳本）
WINDOWS = os.name == "nt"
JAVA_EXE = "java.exe" if WINDOWS else "java"
MVN_EXE = "mvn.cmd" if WINDOWS else "mvn"


def java_version(java: Path) -> int:
    try:
        out = subprocess.run(
            [str(java), "-version"], capture_output=True, text=True, timeout=20
        ).stderr
    except (OSError, subprocess.TimeoutExpired):
        return 0
    m = re.search(r'version "(\d+)', out)
    return int(m.group(1)) if m else 0


def find_java_home() -> Path | None:
    candidates = []
    if os.environ.get("JAVA_HOME"):
        candidates.append(Path(os.environ["JAVA_HOME"]))
    for brew in ("/opt/homebrew", "/usr/local"):
        candidates.append(Path(brew) / "opt/openjdk@21/libexec/openjdk.jdk/Contents/Home")
    if Path("/usr/libexec/java_home").exists():
        r = subprocess.run(
            ["/usr/libexec/java_home", "-v", f"{MIN_JAVA}+"], capture_output=True, text=True
        )
        if r.returncode == 0:
            candidates.append(Path(r.stdout.strip()))
    candidates += sorted(Path("/usr/lib/jvm").glob("*21*")) if Path("/usr/lib/jvm").is_dir() else []
    if which := shutil.which("java"):
        candidates.append(Path(which).resolve().parents[1])
    for home in candidates:
        java = home / "bin" / JAVA_EXE
        if java.is_file() and java_version(java) >= MIN_JAVA:
            return home
    return None


def download(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    t0, last = time.time(), 0.0
    with httpx.stream(
        "GET", url, follow_redirects=True, timeout=httpx.Timeout(60, connect=15)
    ) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with tmp.open("wb") as f:
            for chunk in r.iter_bytes(1 << 16):
                f.write(chunk)
                done += len(chunk)
                if time.time() - last > 0.5 or done == total:
                    last = time.time()
                    speed = done / max(time.time() - t0, 1e-6) / 1e6
                    pct = f"{done / total:6.1%}" if total else ""
                    print(
                        f"\r  下載 {dest.name} {pct} {done / 1e6:5.1f} MB（{speed:.1f} MB/s）",
                        end="",
                        flush=True,
                    )
    print()
    tmp.replace(dest)


def find_maven() -> Path:
    if which := shutil.which("mvn"):
        return Path(which)
    home = SCHED / ".maven" / f"apache-maven-{MAVEN_VERSION}"
    mvn = home / "bin" / MVN_EXE
    if mvn.is_file():
        return mvn
    home.parent.mkdir(parents=True, exist_ok=True)
    archive = home.parent / f"apache-maven-{MAVEN_VERSION}-bin.tar.gz"
    print(f"下載 Maven {MAVEN_VERSION}（Maven Central）")
    download(MAVEN_URL, archive)
    expect = httpx.get(MAVEN_URL + ".sha512", follow_redirects=True, timeout=30).text.split()[0]
    actual = hashlib.sha512(archive.read_bytes()).hexdigest()
    if actual != expect:
        archive.unlink()
        raise SystemExit(
            f"Maven 檔案的 SHA-512 不符（{actual[:16]}… ≠ {expect[:16]}…），已刪除，請重跑"
        )
    print("  ✓ SHA-512 驗證通過")
    with tarfile.open(archive) as tar:
        tar.extractall(home.parent, filter="data")
    archive.unlink()
    mvn.chmod(0o755)
    return mvn


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-tests", action="store_true")
    args = parser.parse_args()

    java_home = find_java_home()
    if java_home is None:
        print(f"找不到 Java {MIN_JAVA} 以上。")
        print("  macOS：brew install openjdk@21")
        print("  Ubuntu／WSL2：sudo apt install openjdk-21-jdk-headless")
        print("沒有 Java 時排程頁會自動改用簡易排程（交期優先派工，不做最佳化）。")
        return 1
    print(f"✓ Java {java_version(java_home / 'bin' / JAVA_EXE)}：{java_home}")
    mvn = find_maven()
    print(f"✓ Maven：{mvn}")

    env = {**os.environ, "JAVA_HOME": str(java_home)}
    env["PATH"] = f"{java_home / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    cmd = [str(mvn), "-B", "-ntp", "package"] + (["-DskipTests"] if args.skip_tests else [])
    print("建置排程服務：" + " ".join(cmd[1:]) + "（第一次會下載 Timefold Solver 等套件）")
    t0 = time.time()
    r = subprocess.run(cmd, cwd=SCHED, env=env)
    if r.returncode != 0:
        print("✗ 建置失敗，請看上方 Maven 輸出")
        return r.returncode
    jar = SCHED / "target" / "scheduler.jar"
    (SCHED / ".java-home").write_text(str(java_home), encoding="utf-8")
    print(
        f"✓ {jar.relative_to(ROOT)}（{jar.stat().st_size / 1e6:.1f} MB，{time.time() - t0:.0f} 秒）"
    )
    print("啟動：make scheduler（或 make demo-all 一起啟動 Ortho2CAD、排程服務與展示伺服器）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
