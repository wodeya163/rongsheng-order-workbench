-- =============================================================
-- 产品询报价管理系统 v2.2 — 全量 DDL
-- 修订时间：2026-11-05 · 依据：张哥第二次口径确认
-- 2026-11-06 补订：订单头 8 字段 / 订单明细 9 字段（v2.3 字段规范，见 core/order_fields.py）。
--   新增列全部带 DEFAULT，老库经 db.SCHEMA_ADDITIONS 自动补齐；
--   「数量」仍唯一映射 qty_ordered(实值)，金额/合计字段绝不参与数量守恒。
-- 2026-11-07 补订：订单排产（v2.4）。取消「新建排产项目」入口，改为在订单明细上右键
--   为该物资建立 order_schedules；一条明细对应一张订单排产单，自动建生产项目 + 守恒关联。
--   图片/图纸/审批文档落在 order_schedule_files。所有物资均有订单排产单且实值排满时，
--   订单状态自动置为「已排产」（保留 status_before 以便回退）。
--
-- 五条硬口径：
--   1. 询配码域与询报价域【完全独立】，没有任何外键互相指向
--   2. 询配码明细数量 qty_reference 是【虚值】，只做统计，
--      禁止参与任何数量守恒 / 排产校验
--   3. 生产排产的唯一数量来源是【订单明细 qty_ordered（实值）】
--   4. ★ 中标项目也要生成排产数量：中标项目 → 生成【一张内部订单】，
--      由内部订单明细(实值 = 中标明细数量)进入同一套排产守恒链路；
--      中标项目本身绝不直接产生排产数量
--   5. ★ 一个询配码项目可产生【多张外部订单】（分批下单），
--      以 orders.batch_seq 标记第几单；虚值不限制下单次数与数量
--
-- 两条链路（共用订单枢纽与同一套守恒规则）：
--   A 询配码项目/文件 → 提交用户 → 订单(外部，可多张，实值) → 排产
--   B 中标招投标项目 → 内部订单(唯一一张，实值) → 排产
--   排产项目另可挂中标项目作为【来源标签】(不携带数量)
-- =============================================================

-- ── 通用支撑 ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS doc_sequence (
    prefix    TEXT NOT NULL,
    period    TEXT NOT NULL,
    next_val  INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (prefix, period)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity      TEXT NOT NULL,
    entity_id   INTEGER,
    action      TEXT NOT NULL,
    before_json TEXT,
    after_json  TEXT,
    operator    TEXT,
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_al_entity ON audit_log(entity, entity_id);

-- ── 域 1：询报价 / 招投标项目统计 ───────────────────────────

CREATE TABLE IF NOT EXISTS bid_projects (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_no        TEXT NOT NULL UNIQUE,        -- ZB-YYYYMM-NNNN
    project_name  TEXT NOT NULL,
    customer      TEXT,
    tender_no     TEXT,                        -- 招标编号
    bid_date      TEXT,
    status        TEXT NOT NULL DEFAULT '草稿',-- 草稿/已报价/中标/未中标/作废
    owner         TEXT,
    currency      TEXT DEFAULT 'CNY',
    remark        TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bp_status ON bid_projects(status);

CREATE TABLE IF NOT EXISTS bid_items (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    bid_project_id     INTEGER NOT NULL REFERENCES bid_projects(id) ON DELETE CASCADE,
    line_no            INTEGER NOT NULL,
    product_name       TEXT NOT NULL,
    spec               TEXT,
    category           TEXT,
    unit               TEXT,
    qty                REAL NOT NULL DEFAULT 1 CHECK (qty > 0),  -- 中标明细数量(实值)，内部订单取此值
    purchase_price     REAL NOT NULL DEFAULT 0,
    quote_ratio        REAL NOT NULL DEFAULT 1 CHECK (quote_ratio > 0),
    is_won             TEXT,                   -- 是 / 否 / 空
    image_path         TEXT,
    drawing_path       TEXT,
    legacy_product_id  INTEGER,                -- v1.5 products.id，迁移溯源
    remark             TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE (bid_project_id, line_no)
);
CREATE INDEX IF NOT EXISTS idx_bi_project ON bid_items(bid_project_id);
CREATE INDEX IF NOT EXISTS idx_bi_legacy  ON bid_items(legacy_product_id);
-- 对外报价 = purchase_price ÷ quote_ratio，计算列不落库（沿用 v1.5 口径）

-- ── 域 2：询配码项目（独立域，与询报价零关联） ──────────────

CREATE TABLE IF NOT EXISTS code_projects (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_no        TEXT NOT NULL UNIQUE,        -- XPM-YYYYMM-NNNN
    project_name  TEXT NOT NULL,
    customer      TEXT,
    status        TEXT NOT NULL DEFAULT '草稿',-- 草稿/已提交/部分下单/已下单/关闭
    submit_date   TEXT,                        -- 询配码文件提交用户日期
    file_path     TEXT,                        -- 提交给用户的询配码文件
    version       TEXT,
    owner         TEXT,
    remark        TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
-- 注意：本表【没有】source_quote_id / source_bid_project_id，与域 1 完全解耦

CREATE TABLE IF NOT EXISTS code_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    code_project_id     INTEGER NOT NULL REFERENCES code_projects(id) ON DELETE CASCADE,
    line_no             INTEGER NOT NULL,
    item_code           TEXT NOT NULL,        -- XPM-YYYYMM-NNNN-02（系统项号/溯源锚点）
    -- ★ 2026-11-06 起，业务字段规范见 v2/core/fields.py（10 列，单一来源）
    --   询价物料名称 / 商品编码 / 物料编码 / 物料名称 / 单位 /
    --   厂家价格(元) / 物料具体情况 / 参考数量 / 备注 / 技术要求
    inquiry_item_name   TEXT,                 -- 询价物料名称（客户原始叫法）
    goods_code          TEXT,                 -- 商品编码
    material_code       TEXT,                 -- 物料编码
    item_name           TEXT NOT NULL,        -- 物料名称（主名称，订单/快照沿用）
    unit                TEXT,
    factory_price       REAL DEFAULT 0,       -- 厂家价格(元)，仅询价信息，非订单单价
    item_detail         TEXT,                 -- 物料具体情况
    qty_reference       REAL DEFAULT 0,       -- ★虚值：参考数量，仅统计展示
    remark              TEXT,
    tech_requirement    TEXT,
    -- 以下为 v2.0 历史字段，保留兼容旧模板/旧数据，不在 10 列主表单展示
    spec                TEXT,
    material            TEXT,
    std_code            TEXT,                 -- 执行标准
    drawing_no          TEXT,
    status              TEXT NOT NULL DEFAULT '待下单',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (code_project_id, item_code),
    UNIQUE (code_project_id, line_no)
);
-- ★ qty_reference 不设 CHECK、不参与任何守恒计算；
--   排产数量与它【不对应】，实值在 order_items.qty_ordered
-- ★ factory_price 也不参与任何金额守恒：订单金额以 order_items.unit_price 为准
CREATE INDEX IF NOT EXISTS idx_ci_project ON code_items(code_project_id);
CREATE INDEX IF NOT EXISTS idx_ci_goods   ON code_items(goods_code);
CREATE INDEX IF NOT EXISTS idx_ci_material ON code_items(material_code);

-- ── 域 3：订单枢纽（排产的唯一数量来源） ────────────────────
-- 两种订单类型，共用 order_items.qty_ordered 实值与同一套守恒规则：
--   外部订单：用户按询配码文件下单，一个询配码项目可产生多张(分批)
--   内部订单：中标项目生成，一个中标项目【仅一张】，数量取中标明细

CREATE TABLE IF NOT EXISTS orders (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_no                 TEXT NOT NULL UNIQUE,   -- DD- 外部 / ND- 内部（系统单号，不可编辑）
    -- ★ 2026-11-06 v2.3 订单头 8 字段，规范见 v2/core/order_fields.py（单一来源）
    --   订单项目编号 / 客户名称 / 订单类型 / 订单状态 / 价税合计 /
    --   交货日期 / 物资数量 / ERP订单编号
    project_no             TEXT,                   -- 订单项目编号（业务订单编号，导入分组键）
    customer               TEXT,                   -- 客户名称
    -- 2026-11-07 v2.9：订单来源（销售渠道）。预设四值 + 允许手输任意值，
    --   不建 CHECK 约束（业务会新增渠道，改约束要重建表）；
    --   取值来源单一：v2/core/order_fields.py::ORDER_SOURCES
    order_source           TEXT,
    order_type             TEXT NOT NULL DEFAULT '外部订单'
                           CHECK (order_type IN ('外部订单', '内部订单')),
    status                 TEXT NOT NULL DEFAULT '草稿',  -- 订单状态
    amount_total           REAL DEFAULT 0,        -- 价税合计（留空自动 = Σ明细总价）
    required_date          TEXT,                   -- 交货日期
    item_qty_total         REAL DEFAULT 0,        -- 物资数量（留空自动 = 明细行数，Q12）
    erp_order_no           TEXT,                   -- ERP订单编号
    -- ★ 2026-11-07 P1-5：显式声明标记。1 = 由用户/导入文件给定，增删明细不再自动覆盖；
    --   0 = 派生值，明细变动后自动重算。界面「恢复自动汇总」会把它们清 0。
    amount_manual          INTEGER NOT NULL DEFAULT 0,
    item_qty_manual        INTEGER NOT NULL DEFAULT 0,
    -- 系统溯源 / 兼容字段（不属于 8 个业务字段，但链路必须保留）
    source_code_project_id INTEGER REFERENCES code_projects(id) ON DELETE SET NULL, -- 外部订单来源，可空(手工订单)
    source_bid_project_id  INTEGER REFERENCES bid_projects(id) ON DELETE SET NULL, -- 内部订单来源，可空
    batch_seq              INTEGER NOT NULL DEFAULT 1,  -- 同一询配码项目下第几单(外部订单)
    order_date             TEXT,
    owner                  TEXT,
    remark                 TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    CHECK (
        (order_type = '内部订单' AND source_code_project_id IS NULL)
        OR
        (order_type = '外部订单' AND source_bid_project_id IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_o_project_no  ON orders(project_no);
CREATE INDEX IF NOT EXISTS idx_o_erp_no      ON orders(erp_order_no);
CREATE INDEX IF NOT EXISTS idx_o_source      ON orders(source_code_project_id);
CREATE INDEX IF NOT EXISTS idx_o_bid_source  ON orders(source_bid_project_id);
CREATE INDEX IF NOT EXISTS idx_o_status      ON orders(status);
CREATE INDEX IF NOT EXISTS idx_o_type        ON orders(order_type);
-- ★ 一个中标项目最多一张内部订单（幂等硬约束；要放开多张时删此索引即可）
CREATE UNIQUE INDEX IF NOT EXISTS uq_internal_order_per_bid
    ON orders(source_bid_project_id) WHERE source_bid_project_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS order_items (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id             INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    line_no              INTEGER NOT NULL,
    -- ★ 2026-11-06 v2.3 订单明细 9 字段，规范见 v2/core/order_fields.py（单一来源）
    --   物资代码 / 物资名称 / 实际生产物资名称 / 规格 / 单位 /
    --   数量 / 单价 / 总价 / 参数描述
    item_code            TEXT,                    -- 物资代码
    item_name            TEXT NOT NULL,           -- 物资名称（主名称，排产/溯源沿用）
    production_name      TEXT,                   -- 实际生产物资名称（留空 = 物资名称）
    spec                 TEXT,                   -- 规格
    unit                 TEXT,                   -- 单位
    qty_ordered          REAL NOT NULL CHECK (qty_ordered > 0),  -- ★数量(实值)：排产数量锚点
    unit_price           REAL DEFAULT 0,         -- 单价
    amount               REAL DEFAULT 0,         -- 总价（留空自动 = 数量 × 单价）
    param_desc           TEXT,                   -- 参数描述
    -- 系统溯源 / 兼容字段
    material             TEXT,
    source_code_item_id  INTEGER REFERENCES code_items(id) ON DELETE SET NULL,  -- 外部订单：来自哪个询配码项
    source_bid_item_id   INTEGER REFERENCES bid_items(id) ON DELETE SET NULL,  -- 内部订单：来自哪个中标明细项
    remark               TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    UNIQUE (order_id, line_no),
    CHECK (NOT (source_code_item_id IS NOT NULL AND source_bid_item_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_oi_order  ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_oi_source ON order_items(source_code_item_id);
CREATE INDEX IF NOT EXISTS idx_oi_bid    ON order_items(source_bid_item_id);

-- ── 域 4：生产排产 ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS production_projects (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_no                 TEXT NOT NULL UNIQUE,  -- SC-YYYYMM-NNNN
    title                  TEXT NOT NULL,
    source_bid_project_id  INTEGER REFERENCES bid_projects(id) ON DELETE SET NULL, -- 仅中标项目可挂(来源标签，不携带数量)
    status                 TEXT NOT NULL DEFAULT '待排',
    plan_start             TEXT,
    plan_end               TEXT,
    actual_start           TEXT,
    actual_end             TEXT,
    workshop               TEXT,
    owner                  TEXT,
    priority               INTEGER DEFAULT 3,
    remark                 TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pp_status ON production_projects(status);
CREATE INDEX IF NOT EXISTS idx_pp_bid    ON production_projects(source_bid_project_id);

CREATE TABLE IF NOT EXISTS production_tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES production_projects(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    doc_no       TEXT,
    process_name TEXT NOT NULL,
    workcenter   TEXT,
    qty          REAL NOT NULL DEFAULT 0,
    finished_qty REAL NOT NULL DEFAULT 0 CHECK (finished_qty >= 0),
    plan_start   TEXT,
    plan_end     TEXT,
    status       TEXT NOT NULL DEFAULT '待开工',
    owner        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (project_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_pt_project ON production_tasks(project_id);

-- ── 打通核心：排产项目 ↔ 订单明细（不是询配码明细！） ───────
-- 数量锚：SUM(allocated_qty) ≤ order_items.qty_ordered
-- 溯源锚 A(外部订单)：order_items.source_code_item_id → code_items → code_projects
-- 溯源锚 B(内部订单)：order_items.source_bid_item_id  → bid_items  → bid_projects

CREATE TABLE IF NOT EXISTS production_links (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id                INTEGER NOT NULL REFERENCES production_projects(id) ON DELETE CASCADE,
    order_item_id             INTEGER NOT NULL REFERENCES order_items(id) ON DELETE RESTRICT,
    allocated_qty             REAL NOT NULL CHECK (allocated_qty > 0),
    order_no_snapshot         TEXT NOT NULL,
    order_line_snapshot       TEXT NOT NULL,
    order_type_snapshot       TEXT NOT NULL DEFAULT '外部订单',
    source_type_snapshot      TEXT,        -- 询配码项目 / 中标项目 / 订单新增
    item_name_snapshot        TEXT,
    spec_snapshot             TEXT,
    code_no_snapshot          TEXT,        -- 来自哪个询配码项目（外部订单，可空）
    item_code_snapshot        TEXT,        -- 来自该询配码的哪一项（可空）
    code_item_name_snapshot   TEXT,
    qty_reference_snapshot    REAL,        -- 仅展示虚值，不参与校验
    bid_no_snapshot           TEXT,        -- 来自哪个中标项目（内部订单，可空）
    bid_line_snapshot         TEXT,        -- 来自该中标项目的哪一项
    bid_item_name_snapshot    TEXT,
    created_by                TEXT,
    created_at                TEXT NOT NULL,
    UNIQUE (project_id, order_item_id)
);
CREATE INDEX IF NOT EXISTS idx_pl_item    ON production_links(order_item_id);
CREATE INDEX IF NOT EXISTS idx_pl_project ON production_links(project_id);
CREATE INDEX IF NOT EXISTS idx_pl_type    ON production_links(order_type_snapshot);

-- ── 订单排产（2026-11-07 v2.4）──────────────────────────────
-- 入口：订单管理 → 选中订单 → 下方明细某一物资 → 右键「订单排产」
-- 一条订单明细【最多一张】订单排产单：重复右键即打开既有单据补附件/审批。
-- project_id / link_id 指向自动创建的生产项目与守恒关联，数量口径完全不变：
--   Σ production_links.allocated_qty ≤ order_items.qty_ordered
-- status_before 记录建单前订单状态，供「不再全排」时原样回退。
--
-- v3.0 新增排产范围标记（scope / batch_no）：
--   分项  一条物资一张排产单、一份审批文档（默认，历史数据）；
--   合并  同一批次多张排产单共用一张审批文档（明细多选合并排产）；
--   整单  订单全部物资同批次共用一张审批文档（整单排产）。
--   batch_no 相同 = 同一批排产，物理审批文件只存一份、登记到批次内每张排产单。

CREATE TABLE IF NOT EXISTS order_schedules (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_no            TEXT NOT NULL UNIQUE,          -- DDPS-YYYYMM-0001
    order_id          INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    order_item_id     INTEGER NOT NULL UNIQUE REFERENCES order_items(id) ON DELETE CASCADE,
    project_id        INTEGER REFERENCES production_projects(id) ON DELETE SET NULL,
    link_id           INTEGER REFERENCES production_links(id) ON DELETE SET NULL,
    project_created   INTEGER NOT NULL DEFAULT 0,  -- 1=本单自动建的生产项目，撤销时可一并回收
    allocated_qty     REAL NOT NULL DEFAULT 0,      -- 本次排产实值（快照，不参与守恒）
    status            TEXT NOT NULL DEFAULT '待审批',
    status_before     TEXT NOT NULL DEFAULT '',     -- 自动置「已排产」前的订单状态
    scope             TEXT NOT NULL DEFAULT '分项', -- 分项 / 合并 / 整单（排产范围标记）
    batch_no          TEXT,                        -- 合并/整单批次号，同批次共用一张审批单
    remark            TEXT NOT NULL DEFAULT '',
    created_by        TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_os_order ON order_schedules(order_id);
CREATE INDEX IF NOT EXISTS idx_os_item  ON order_schedules(order_item_id);
CREATE INDEX IF NOT EXISTS idx_os_batch ON order_schedules(order_id, batch_no);

CREATE TABLE IF NOT EXISTS order_schedule_files (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id  INTEGER NOT NULL REFERENCES order_schedules(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,                    -- 图片 / 图纸 / 审批
    file_name    TEXT NOT NULL,
    file_path    TEXT NOT NULL,                    -- 复制到 data/订单排产附件/ 后的绝对路径
    file_size    INTEGER NOT NULL DEFAULT 0,
    created_by   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_osf_schedule ON order_schedule_files(schedule_id);

-- ── v2.8：查询方案（P2-5）────────────────────────────────────
-- 保存常用筛选条件，便于「上月待发货」「我的逾期单」这类固定视角一键切换。
CREATE TABLE IF NOT EXISTS query_presets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,              -- 方案名（同名覆盖走服务层判断）
    view_key    TEXT NOT NULL DEFAULT '',          -- 适用视图：bids/codes/orders/...，空=通用
    filters     TEXT NOT NULL DEFAULT '{}',        -- JSON：与 QueryPanel.values() 同结构
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qp_view ON query_presets(view_key);

-- ── v3.4.0：采购合同管理（生产排产操作模块）──────────────────
-- 与订单是「可空关联」：合同可挂某张订单，也可独立登记；合同物资清单由该订单
-- 明细即时读取生成，不复制数量，避免制造第二个数量源（守恒口径零改动）。
-- 附件物理落在 data/采购合同/<合同号>/，库里只存相对 data/ 的 posix 路径。
-- 本段 DDL 每次启动都会执行（IF NOT EXISTS），老库自动补建新表。
-- v3.5.0：新增合同物资明细桥表与两列口径标记，schema_meta 升至 v7（迁移只回填、
--   不改动任何既有业务数据）。合同与订单明细改为【多对多】：一张订单的多项物资
--   可分签不同供应商，一份合同也可覆盖多张订单的物资（合并采购）。
CREATE TABLE IF NOT EXISTS purchase_contracts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_no         TEXT NOT NULL UNIQUE,               -- CG-YYYYMM-NNNN
    contract_name  TEXT NOT NULL,                     -- 合同名称
    contract_type  TEXT NOT NULL DEFAULT '物资采购',   -- 物资采购/外协加工/设备采购/其他
    order_id       INTEGER REFERENCES orders(id) ON DELETE SET NULL,
    supplier       TEXT NOT NULL DEFAULT '',          -- 供方（供应商）
    buyer          TEXT DEFAULT '',                   -- 需方（采购方）
    amount         REAL NOT NULL DEFAULT 0,           -- 合同金额（元）
    currency       TEXT NOT NULL DEFAULT 'CNY',
    -- v3.5.0 口径标记：1 = 含税（全系统统一含税），0 = 不含税（混用时不出利润率）
    tax_included   INTEGER NOT NULL DEFAULT 1,
    -- v3.5.0 合同金额来源：doc = 文档识别 / manual = 人工录入 / items = 明细汇总
    amount_source  TEXT NOT NULL DEFAULT 'doc',
    sign_date      TEXT,                              -- 签订日期 YYYY-MM-DD
    delivery_date  TEXT,                              -- 交货日期 YYYY-MM-DD
    status         TEXT NOT NULL DEFAULT '草稿',      -- 草稿/已签订/执行中/已完成/作废
    payment_terms  TEXT DEFAULT '',                   -- 付款方式
    contact        TEXT DEFAULT '',                   -- 联系人
    remark         TEXT NOT NULL DEFAULT '',
    created_by     TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pc_order  ON purchase_contracts(order_id);
CREATE INDEX IF NOT EXISTS idx_pc_status ON purchase_contracts(status);

CREATE TABLE IF NOT EXISTS purchase_contract_files (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id  INTEGER NOT NULL REFERENCES purchase_contracts(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL DEFAULT '合同附件',   -- 合同正文 / 合同附件 / 扫描件 / 其他
    file_name    TEXT NOT NULL,
    file_path    TEXT NOT NULL,                      -- 相对 data/ 的 posix 路径
    file_size    INTEGER NOT NULL DEFAULT 0,
    created_by   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pcf_contract ON purchase_contract_files(contract_id);

-- ── v3.5.0：采购合同物资明细（订单盈亏 / 多供应商归集）──────────
-- ★ 设计口径（张哥 2026-11-18 拍板）：
--   1. 不在 order_items 上加「采购单价」列 —— 一条物资可能分两家供应商、分两批
--      采购，单值表达不了；采购成本是【按合同汇总的派生值】，落桥表由 GROUP BY 得出，
--      避免制造第二个金额源（与数量守恒唯一来源 qty_ordered 的硬口径一致）。
--   2. 金额统一【含税】：unit_price 是含税采购单价，amount = q2(qty × unit_price)；
--      tax_rate 仅作记录，不参与利润率计算（口径混用时服务层只出金额并告警）。
--   3. 订单明细可拆给多份合同：UNIQUE(contract_id, order_item_id)，
--      数量守恒 Σ qty ≤ order_items.qty_ordered 由 core/integrity.py 校验。
--   4. 两侧外键都 CASCADE：删订单 / 删明细 / 删合同时分配行自然消失，
--      不阻断既有删除链路；删除动作由服务层留审计。
CREATE TABLE IF NOT EXISTS purchase_contract_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id    INTEGER NOT NULL REFERENCES purchase_contracts(id) ON DELETE CASCADE,
    order_item_id  INTEGER NOT NULL REFERENCES order_items(id) ON DELETE CASCADE,
    qty            REAL NOT NULL CHECK (qty > 0),
    unit_price     REAL NOT NULL DEFAULT 0,        -- 含税采购单价
    tax_rate       REAL NOT NULL DEFAULT 0,        -- 税率（仅记录）
    amount         REAL NOT NULL DEFAULT 0,        -- 含税金额，q2 量化
    currency       TEXT NOT NULL DEFAULT 'CNY',
    remark         TEXT DEFAULT '',
    created_by     TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE (contract_id, order_item_id)
);
CREATE INDEX IF NOT EXISTS idx_pci_contract ON purchase_contract_items(contract_id);
CREATE INDEX IF NOT EXISTS idx_pci_item     ON purchase_contract_items(order_item_id);
