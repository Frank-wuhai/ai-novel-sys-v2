"""Deprecated OpenClaw story initialization script.

OpenClaw is no longer used as the story-system brain. Keep this file only as a
compatibility warning for older experiments. Use `python -m app.cli ...` for
production-system operations.
"""


def main() -> None:
    print("已弃用：不要用 OpenClaw 初始化角色/世界观。")
    print("请改用：python -m app.cli create-book / create-foundation / create-chapter-brief")
    print("OpenClaw 后续只应负责平台发布、浏览器操作、截图和反馈采集。")


if __name__ == "__main__":
    main()

