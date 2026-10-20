import os

# 从脚本自身位置推出仓库根，这样镜像里（/app）也能跑同一份扫描。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".venv", "__pycache__", ".git", ".tmp_pptx", "node_modules"}
TEXT_EXT = {".py", ".md", ".csv", ".json", ".txt", ".sh", ".log", ".example", ".bat", ".skill", ".toml", ".yaml", ".yml"}
SECRET_NAMES = ("KEY", "TOKEN", "SECRET", "PASSWORD")

env = {}
# .env 被 .dockerignore 排除，镜像里没有它；缺了就跳过"值是否出现在别处"这一项，其余扫描照跑。
if os.path.isfile(os.path.join(ROOT, ".env")):
    with open(os.path.join(ROOT, ".env"), encoding="utf-8") as f:
        for line in f:
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()

secrets = {k: v for k, v in env.items() if any(s in k.upper() for s in SECRET_NAMES) and len(v) >= 8}
nonsecrets = {k: v for k, v in env.items() if k not in secrets and len(v) >= 8}
print("按变量名判定为敏感项 =", sorted(secrets))
print("只作参考的非敏感配置项 =", sorted(nonsecrets))

files = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
    for name in filenames:
        if name == ".env" or os.path.splitext(name)[1].lower() not in TEXT_EXT:
            continue
        path = os.path.join(dirpath, name)
        try:
            with open(path, encoding="utf-8") as handle:
                files.append((os.path.relpath(path, ROOT), handle.read()))
        except (UnicodeDecodeError, OSError):
            pass

print(f"扫描文本文件数 = {len(files)}（排除 .venv 与 .env 本身）")
for var, value in secrets.items():
    leaked = [p for p, body in files if value in body]
    print(f"敏感变量 {var}: 在 {len(leaked)} 个文件中出现 -> {leaked if leaked else '无（0 次）'}")

for var, value in nonsecrets.items():
    where = [p for p, body in files if value in body]
    print(f"非敏感变量 {var}: 出现在 {len(where)} 个文件（不构成泄露，仅说明上一轮的计数口径）")
