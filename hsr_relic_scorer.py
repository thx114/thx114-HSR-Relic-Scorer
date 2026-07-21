"""崩铁遗器实时计分 —— 主入口。

代码按职责拆分到多个模块：
- utils.py         通用工具：日志、路径、系统工具、区域/坐标/字体格式化、截图
- models.py        数据模型：遗器/角色属性/角色配置数据类、序列化、公式求值
- ocr_utils.py     OCR：Umi-OCR 客户端、坐标工具、遗器/面板解析、强化标记检测
                     （参考 https://github.com/hiroi-sora/Umi-OCR 的 ocr/ tbpu/ 模块划分）
- scoring.py       评分：伤害计算、完美遗器基准、归一化评分、评分等级
- overlay.py       叠加层：透明置顶窗口、鼠标穿透、评分显示、ACE 扫光动画
- app.py           应用主窗口：角色配置、遗器管理、热键监听、OCR 调度
"""
from __future__ import annotations

from utils import enable_dpi_awareness, is_admin, relaunch_as_admin


if __name__ == "__main__":
    # 管理员模式启动以保证全局热键
    enable_dpi_awareness()
    if not is_admin():
        relaunch_as_admin()
        raise SystemExit(0)
    # 延迟导入，确保上面两个系统调用不依赖 App 模块
    from app import App
    App().mainloop()
