# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules

datas = [('E:\\AI作品\\Marvis\\Output\\产品询报价管理系统\\v2\\ui\\app.ico', 'v2/ui'), ('E:\\AI作品\\Marvis\\Output\\产品询报价管理系统\\v2\\core\\schema.sql', 'v2/core')]
hiddenimports = ['docx', 'openpyxl', 'pytesseract', 'PIL', 'pdfplumber', 'pypdfium2', 'v2.core.docread', 'v2.core.excelio', 'v2.core.fields', 'v2.core.order_fields', 'v2.ui.dialogs', 'v2.ui.theme', 'v2.ui.query_panel', 'v2.ui.datefield', 'v2.core.uistate', 'v2.ui.customerfield', 'v2.ui.partyfield', 'v2.core.contract_amount', 'win32com.client', 'v2.domains.profit', 'v2.ui.profit_chart', 'v2.ui.profit_dialog', 'v2.ui.contract_items_dialog', 'v2.core.contract_items', 'v2.core.contract_prefs', 'v2.ui.panels.profit_actions', 'v2.core.duereminder', 'v2.core.reminder_prefs', 'v2.ui.due_dialog', 'v2.ui.panels.due_actions', 'v2.ui.base_dialog', 'v2.domains.maintenance', 'v2.ui.schedule_files_dialog', 'v2.ui.contract_dialog', 'v2.ui.panels.contract_actions', 'v2.domains.contract', 'v2.core.applog', 'v2.core.current_user', 'v2.core.money', 'v2.core.query', 'v2.core.single_instance', 'v2.core.updater', 'v2.ui.update_dialog', 'v2.ui.async_task', 'v2.demo_data']
datas += collect_data_files('docx')
hiddenimports += collect_submodules('v2.core')
hiddenimports += collect_submodules('pdfminer')
hiddenimports += collect_submodules('pdfplumber')


a = Analysis(
    ['E:\\AI作品\\Marvis\\Output\\产品询报价管理系统\\v2\\app_v2.py'],
    pathex=['E:\\AI作品\\Marvis\\Output\\产品询报价管理系统'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pandas', 'numpy', 'pyarrow', 'numba', 'llvmlite', 'scipy', 'matplotlib', 'fitz', 'pymupdf', 'IPython', 'jupyter', 'notebook', 'pytest', 'sqlalchemy', 'tkinter', 'PIL.ImageQt', 'pygame', 'SDL2', 'Pythonwin', 'pywin'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='订单管理工作台',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='E:\\AI作品\\Marvis\\Output\\产品询报价管理系统\\version_info.txt',
    icon=['E:\\AI作品\\Marvis\\Output\\产品询报价管理系统\\v2\\ui\\app.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='订单管理工作台',
)
