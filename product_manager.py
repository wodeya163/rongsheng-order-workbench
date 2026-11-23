#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
产品询报价管理系统 - 兼容入口

v1.3 起主逻辑已迁移至 app/ 包（分层架构）。
本文件保留为统一入口，以兼容 启动管理系统.bat 与 PyInstaller 打包配置。
旧版完整源码已备份为 product_manager_legacy.py。
"""
from app.main import main

if __name__ == "__main__":
    main()
