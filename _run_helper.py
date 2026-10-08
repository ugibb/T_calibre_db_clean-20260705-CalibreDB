"""Windows bat 辅助脚本 —— 处理配置读取、批次检测、CLI 调用。"""
import json
import glob
import os
import subprocess
import sys

CONFIG_PATH = "06-src/template/pipeline_config.json"
CLI_PATH = "06-src/knowledge_assets/preprocess/cli.py"


def read_config(key, subkey):
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)
    print(config[key][subkey])


def detect_batch(input_dir):
    files = sorted(glob.glob(os.path.join(input_dir, "metadata-*.db")))
    if files:
        stem = os.path.splitext(os.path.basename(files[-1]))[0]
        key = stem.replace("metadata-", "")
        print(f"{key[0:4]}-{key[4:6]}-{key[6:8]}")


def latest_db(directory, pattern):
    """输出目录下匹配 pattern 的最后一个文件路径（空则无输出）。"""
    files = sorted(glob.glob(os.path.join(directory, pattern)))
    if files:
        print(files[-1])


def run_cli(step, phase, batch, extra):
    has_batch = any(a == "--batch" or a.startswith("--batch=") for a in extra)
    args = [] if has_batch else ["--batch", batch]
    args.extend(extra)

    cmd = [sys.executable, CLI_PATH, step]
    if phase:
        cmd.append(phase)
    cmd.extend(args)

    print(f"\n───── {' '.join(cmd)}\n")
    sys.exit(subprocess.call(cmd))


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else ""

    if command == "_cfg":
        read_config(sys.argv[2], sys.argv[3])
    elif command == "_detect_batch":
        detect_batch(sys.argv[2])
    elif command == "_latest_db":
        latest_db(sys.argv[2], sys.argv[3])
    elif command == "_run":
        step = sys.argv[2]
        phase = sys.argv[3] if len(sys.argv) > 3 else ""
        batch = sys.argv[4] if len(sys.argv) > 4 else ""
        extra = sys.argv[5:] if len(sys.argv) > 5 else []
        run_cli(step, phase, batch, extra)


if __name__ == "__main__":
    main()
