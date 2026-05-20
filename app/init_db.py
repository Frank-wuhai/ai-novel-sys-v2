from app.db.init import init_db
from app.models.entities import Character, Foreshadow, Chapter, WorldRule as WorldSetting


if __name__ == "__main__":
    init_db()
    print("数据库初始化完成")
