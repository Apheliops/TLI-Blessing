# -*- coding: utf-8 -*-
"""
Arayüz dili (v6.1): uygulamanın ve küçük panelin kullanıcıya görünen yazıları.
Sadece ekrana basılan metin çevrilir; ekran okuma, log, CSV, hata raporu hep İngilizce kalır.

Kullanım: tr("Recent purchases") -> seçili dilde. Program içinde metinler İngilizce kurulur, ekrana
basılırken çevrilir. Kalıplar ("{n} per run") süslü parantezli yerleri yakalar; yakalanan parça da
(ör. "Armament") ayrıca çevrilir. Kalıba uymayan " · " ile ayrılmış metin parça parça çevrilir,
kalanlar sözlükteki terimlerle değiştirilir.
"""
import re

LANGS = [("en", "English"), ("zh", "简体中文")]
_LANG = {"code": "en"}
MISSING = set()          # (test için) çevirisi bulunamayan metinler


def set_lang(code):
    _LANG["code"] = code if code in dict(LANGS) else "en"


def lang():
    return _LANG["code"]


# --------------------------------------------------------------------------- #
# Terimler: oyunun Çince adlarıyla (tlidb.com/cn ve oyuncunun ekran görüntüleri)
# --------------------------------------------------------------------------- #
GLOSSARY_ZH = {
    # eşyalar
    "Flame Elementium": "初火源质", "Flame Sand": "初火灵砂", "Energy Core": "能量核心",
    "Netherrealm Resonance": "异界回响", "Netherrealm resonance": "异界回响",
    "Transcendence Wedge (Legendary)": "升华之楔（传奇）", "Transcendence Wedge (Rare)": "升华之楔（稀有）",
    "Transcendence Wedge (Magic)": "升华之楔（魔法）", "Ultimate Ember": "至臻灰烬",
    "Divinity Stone": "神威辉石", "Arcane Stone": "奥秘辉石",
    # Void Sea
    "Armament": "军械", "Wonder": "奇珍", "Wealth": "财富", "Boss": "首领",
    "Appear chance": "出现概率", "Memory quantity": "追忆数量", "Flat extra": "固定额外",
    "Lucky extra": "概率额外", "Base quantity": "基础数量", "Treasure quantity": "宝藏数量",
    "Rarity": "稀有度", "Replication": "复制", "No effect on red": "不影响红色",
    "Mem qty": "追忆数量", "Appear": "出现概率", "Flat": "固定额外", "Lucky": "概率额外",
    "Base qty": "基础数量", "Treasure": "宝藏数量", "None": "无",
    "Insufficient": "数据不足", "Low": "低", "Preliminary": "初步", "Good": "可靠",
    "left": "左", "middle": "中", "right": "右", "Left": "左", "Middle": "中", "Right": "右",
    "blue": "蓝", "purple": "紫", "yellow": "黄", "Normal": "普通", "Magic": "魔法", "Rare": "稀有",
    "Epic": "传奇",
}

# Tam eşleşen metinler
EXACT_ZH = {
    # üst alan / sekmeler
    "📂  Select log": "📂  选择日志", "Release page": "发布页面", "⬇  Install update": "⬇  安装更新",
    "◈  DS Calculator": "◈  神威辉石计算", "◎  Void Sea Terminal": "◎  虚海终港",
    "Reading log…": "正在读取日志…", "Language": "语言",
    # DS sekmesi
    "1 FE on the market": "市场上 1 初火源质", "Break-even": "保本价", "DS balance": "神威辉石余额",
    "DS price updated": "神威辉石价格更新于", "market price": "市场价", "from the game log": "来自游戏日志",
    "no price yet": "暂无价格",
    "Offers on screen": "屏幕上的商品",
    "Blessings read from the game screen. Open or refresh Blessings to read them automatically.":
        "从游戏画面读取众神所赐。打开或刷新众神所赐即可自动读取。",
    "👁  Read screen": "👁  读取屏幕", "Not read yet.": "尚未读取。", "Reading…": "正在读取…",
    "Items sold for DS": "用神威辉石购买的物品",
    "Click a row to recommend it or hide it from the advice.": "点击一行：推荐或从建议中隐藏。",
    "Recent purchases": "最近购买", "Read automatically from the game log.": "自动从游戏日志读取。",
    "#": "#", "Item": "物品", "Price DS": "价格", "Max DS": "最高价", "Verdict": "结论",
    "Packs": "数量", "Value (FE, best pack)": "价值（初火源质，最佳）", "Recommend": "推荐",
    "Time": "时间", "DS": "神威辉石", "Result": "结果",
    "●  Recommend": "●  推荐", "○  Hidden": "○  隐藏",
    "—  (search on Exchange)": "—  （请在交易所搜索）",
    "Tax %": "税 %", "Min profit %": "最低利润 %", "Auto-read on refresh": "刷新时自动读取",
    "Start with Windows": "开机启动", "Check for updates": "检查更新", "Screen #": "屏幕 #",
    "Quit app": "退出程序",
    "Sold out": "已售罄", "Arcane Stone  (excluded)": "奥秘辉石（已排除）",
    "Bound (unsellable)  (excluded)": "绑定（不可出售，已排除）",
    "→ check manually": "→ 请手动确认", "read · not recommended": "已读取 · 不推荐",
    "price not read": "价格未读取", "no market price": "无市场价", "no price": "无价格",
    "item not read": "物品未读取",
    # küçük panel
    "Blessings": "众神所赐", "Void Sea": "虚海", " Read ": " 读取 ", "Picture": "画面", "LOG": "日志",
    "  Blessings · profitable offers": "  众神所赐 · 有利可图的商品",
    "Not read yet.\nOpen or refresh Blessings.": "尚未读取。\n请打开或刷新众神所赐。",
    "No Divinity Stone price.\nSearch it once on the Exchange.": "没有神威辉石价格。\n请在交易所搜索一次。",
    "No profitable offers.": "没有有利可图的商品。",
    "error, try again": "出错，请重试", "reading…": "读取中…",
    "   → press “Read” again or check the card yourself": "   → 请再次点击“读取”或自行确认该卡",
    "Open the Void Sea Terminal or press “Read”.": "打开虚海终港或点击“读取”。",
    "Terminal is full (6/6).": "终港已满（6/6）。",
    "Advice appears here when the selection screen opens.": "打开选择界面时会在这里显示建议。",
    "Flames are almost equal": "两种夜火几乎相同", "No effect on red": "不影响红色",
    "Almost equal": "几乎相同",
    "(if your pick is not added automatically, click it)": "（如果你的选择没有自动加入，请点击它）",
    "Could not tell which card was picked: click the card to add it": "无法判断选了哪张卡：请点击该卡加入",
    # Void Sea sekmesi (ana pencere)
    "Runs logged": "已记录次数", "Hero Memories": "英雄追忆", "Red memories": "红色追忆", "Last run": "上一次",
    "tracked by the app": "由程序记录", "no runs yet": "暂无记录", "—": "—",
    "Hero Memories per run": "每次的英雄追忆",
    "Last 20 runs, newest on the right. The dashed line is your average. Click a bar to open that run.":
        "最近 20 次，最新的在右边。虚线是你的平均值。点击柱形查看那一次。",
    "Run history": "记录", "Whisper types compared": "秘语类型对比",
    "Newest first. Click a run to see its whispers.": "最新的在前。点击一次记录查看它的秘语。",
    "Whispers are often picked together, so “with / without” averages can mislead; the estimated effect separates them.":
        "秘语常常一起选择，所以“有 / 无”的平均值可能误导；估计效果会把它们分开。",
    "Date": "日期", "Whispers": "秘语", "Memories": "追忆", "Red": "红色",
    "Whisper type": "秘语类型", "Runs with / w/o": "有 / 无 次数", "Avg with": "有时平均", "Avg w/o": "无时平均",
    "Estimated effect": "估计效果", "Confidence": "可信度", "Red / run": "红色 / 次",
    "insufficient data": "数据不足", "no effect on count": "不影响数量", "red only": "只影响红色",
    "Estimated effect = memories one typical whisper adds to a run (range: 10–90% when the runs are resampled).":
        "估计效果 = 一条典型秘语每次增加的追忆数（范围：重新抽样时的 10–90%）。",
    "Analysis summary": "分析摘要", "No runs yet": "暂无记录", "No runs yet.": "暂无记录。",
    "Enter the Void Sea Terminal in game; each run is added here automatically.": "在游戏中进入虚海终港；每次都会自动加入这里。",
    "No run selected": "未选择记录", "Click a run in the table or a bar in the chart.": "点击表格中的一行或图表中的柱形。",
    "Red memories are counted with the +1 / −1 buttons on the overlay's Void Sea tab.":
        "红色追忆用小窗口虚海页上的 +1 / −1 按钮计数。",
    "Whispers were not read for this run.": "这次的秘语没有读取到。",
    "Combinations seen in 3+ runs (average memories):": "出现 3 次以上的组合（平均追忆）：",
    "whispers not read": "秘语未读取", "Whispers not read": "秘语未读取",
    "run still in progress or the app was closed during the run": "这次仍在进行，或程序在进行中被关闭",
    # güncelleme
    "Install update": "安装更新",
    "Update": "更新", "(pick a row)": "（请选择一行）", "(no selection)": "（未选择）", "TLI Blessing Helper": "TLI Blessing Helper",
}

# Kalıplar: {ad} yerleri yakalanır, yakalanan parça da çevrilir
TEMPLATES_ZH = {
    "{n} per run": "每次 {n}", "{n} mem": "{n} 追忆", "{r} red · {t}": "{r} 红色 · {t}",
    "after {tax}% tax": "扣除 {tax}% 税后",
    "⚠ {d} · may be outdated": "⚠ {d} · 价格可能过时",
    "Divinity Stone costs {p} FE each. Prices come from your Exchange searches in the game log.":
        "神威辉石每个 {p} 初火源质。价格来自游戏日志中你在交易所的搜索。",
    "Divinity Stone costs {p} FE each. Prices come from your Exchange searches in the game log.  Search “Divinity Stone” on the Exchange again to update it.":
        "神威辉石每个 {p} 初火源质。价格来自游戏日志中你在交易所的搜索。  请在交易所再次搜索“神威辉石”来更新。",
    "No Divinity Stone price yet. Open the Exchange in game and search “Divinity Stone”; the price is learned automatically.":
        "还没有神威辉石价格。请在游戏中打开交易所搜索“神威辉石”，价格会自动记录。",
    "Purchases {n} ({ds} DS)  ·  Refreshes {r} ({rd} DS)": "购买 {n}（{ds} 神威辉石）  ·  刷新 {r}（{rd} 神威辉石）",
    "Value received {v} FE  −  DS spent (in FE) {c} FE  =  NET {net} FE": "获得价值 {v}  −  花费的神威辉石（折合初火源质）{c}  =  净 {net}",
    "Value received {v} FE  −  DS spent (in FE) {c} FE  =  NET {net} FE   (price unknown for {u} purchases)":
        "获得价值 {v}  −  花费的神威辉石（折合初火源质）{c}  =  净 {net}   （{u} 次购买没有价格）",
    "✔ BUY  +{p} FE": "✔ 买  +{p} 初火源质", "✖ SKIP  {p}": "✖ 不买  {p}", "✖ SKIP  {p} FE": "✖ 不买  {p} 初火源质",
    "✔ +{p} FE": "✔ +{p} 初火源质", "✖ {p} FE": "✖ {p} 初火源质",
    "not read: {t}": "未读取：{t}", "{item} ×{q}": "{item} ×{q}",
    "✔ Reading log · {n} lines · last write {t}": "✔ 正在读取日志 · {n} 行 · 最后写入 {t}",
    "✖ LOG NOT FOUND — use 'Select log…' on the right to pick UE_game.log": "✖ 找不到日志 — 请点击右侧“选择日志”选择 UE_game.log",
    "✖ {e} — use 'Select log…' to pick UE_game.log": "✖ {e} — 请点击“选择日志”选择 UE_game.log",
    "✖ Log can't be used — the game isn't writing details (Enable Log is off)": "✖ 日志无法使用 — 游戏没有写入详细信息（“开启日志”未打开）",
    "{t} read": "{t} 已读取", "{n} profitable offer(s)": "{n} 个有利可图的商品",
    "{n} card(s) not read, try 'Read screen' again": "{n} 张卡未读取，请再次点击“读取屏幕”",
    "no price: {items} (search on Exchange)": "无价格：{items}（请在交易所搜索）",
    "⚠ {t}: could not read the Blessings screen (is it open? is 'Screen #' right?) — try again.":
        "⚠ {t}：无法读取众神所赐界面（是否已打开？“屏幕 #”是否正确？）— 请重试。",
    "⚠ Screen capture failed: {e}": "⚠ 截图失败：{e}", "⚠ OCR error: {e}": "⚠ 文字识别错误：{e}",
    "  Blessings · {n} profitable": "  众神所赐 · {n} 个有利可图",
    "⚠ {t} could not read the Blessings screen — press “Read”": "⚠ {t} 无法读取众神所赐界面 — 请点击“读取”",
    "⚠ {t}  {a}/{b} cards read — MAY BE INCOMPLETE": "⚠ {t}  已读取 {a}/{b} 张卡 — 可能不完整",
    "✔ {t}  {a}/{b} cards read": "✔ {t}  已读取 {a}/{b} 张卡",
    "   #{n}: {p}": "   #{n}：{p}",
    "⚠ No market price: {items} — search on Exchange": "⚠ 无市场价：{items} — 请在交易所搜索",
    "#{n}  {item}": "#{n}  {item}",
    "{p} DS   →   +{f} FE profit": "{p} 神威辉石   →   利润 +{f} 初火源质",
    "item name not recognized (read: “{t}”)": "物品名称未识别（读取到：“{t}”）",
    "item name not recognized (name not read)": "物品名称未识别（名称未读取）",
    "price not read: {why}": "价格未读取：{why}",
    "price text not visible (card covered or screen moving)": "看不到价格（卡片被遮挡或画面在移动）",
    "digits could not be separated": "数字无法分开", "digits not recognized": "数字无法识别",
    "digits uncertain (two methods said {a} / {b})": "数字不确定（两种方法分别读取为 {a} / {b}）",
    "digits uncertain (maybe: {a})": "数字不确定（可能是：{a}）",
    # Void Sea panel
    "Void Sea · {n}/6": "虚海 · {n}/6", "Red (last run): {n}": "红色（上一次）：{n}",
    "Memories (last run): {n}  · automatic": "追忆（上一次）：{n}  · 自动",
    "Log: {n} runs · {r} red ({a}/run) · {m} memories ({am}/run)": "记录：{n} 次 · {r} 红色（每次 {a}）· {m} 追忆（每次 {am}）",
    "Learning: {n} runs · {m} memories — advice tuned to your log": "学习：{n} 次 · {m} 追忆 — 建议已按你的记录调整",
    "Learning: {n}/{k} runs — then advice is tuned to your log": "学习：{n}/{k} 次 — 之后建议会按你的记录调整",
    "Success threshold: {t} memories (no red and this many or fewer = failure) · {ok} successful / {f} failed":
        "成功标准：{t} 追忆（没有红色且不超过此数 = 失败）· 成功 {ok} / 失败 {f}",
    "Target combination: {c}": "目标组合：{c}", "Option {i}: {c}": "选项 {i}：{c}",
    "{n}× {e}": "{n}× {e}",
    "From your log: {x}": "根据你的记录：{x}",
    "{e} stronger than expected (×{c})": "{e} 比预期强（×{c}）", "{e} weaker than expected (×{c})": "{e} 比预期弱（×{c}）",
    "rarity exponent {k}": "稀有度指数 {k}",
    "⚠ The “Seal Whispers Owned” panel on the right was not read (is it closed?). The advice is based on the last {n} whispers read. Open the panel and press “Read”.":
        "⚠ 右侧的“现有封印秘语”面板没有读取到（是否关闭了？）。建议基于最后读取的 {n} 条秘语。请打开面板并点击“读取”。",
    "⚠ The “Seal Whispers Owned” panel on the right was not read (is it closed?). The advice assumes you own no whispers. Open the panel and press “Read”.":
        "⚠ 右侧的“现有封印秘语”面板没有读取到（是否关闭了？）。建议假设你没有任何秘语。请打开面板并点击“读取”。",
    "{n} more whisper(s) needed for the Terminal": "还需要 {n} 条秘语才能开启终港",
    "▶ Put in the {s} flame": "▶ 放入{s}夜火",
    "{f} flame: average {p}% red": "{f}夜火：红色平均 {p}%",
    "{s} ({c}, {q}+)": "{s}（{c}，{q}+）",
    "Currently selected: {s}": "当前选择：{s}", "Currently selected: {s}  → change it": "当前选择：{s}  → 请更换",
    "▶ {p} · {s}": "▶ {p} · {s}",
    "{p}  {s}: {d}   {g}%": "{p}  {s}：{d}   {g}%",
    "Advice based on these picks: {x}": "建议基于这些选择：{x}",
    "Reading… (incomplete, retry {n}/2)": "正在读取…（不完整，重试 {n}/2）",
    "⚠ {t} some cards were not read — press “Read”": "⚠ {t} 有些卡没有读取到 — 请点击“读取”",
    "⚠ {t} not read — press “Read” while the screen is open": "⚠ {t} 未读取 — 请在界面打开时点击“读取”",
    "read {a}/{b} of the owned whispers": "已读取 {a}/{b} 条现有秘语",
    "Entered the Terminal: {x}": "已进入终港：{x}",
    # nedenler (void_sea.reason_*)
    "Why: none of the cards raises the memory count or the red rate.": "原因：没有一张卡能提高追忆数量或红色概率。",
    "Why: {a} ({ga}%) and {b} ({gb}%) differ by less than 3%; either is fine.": "原因：{a}（{ga}%）和 {b}（{gb}%）相差不到 3%，选哪个都可以。",
    "Why: {a} → {c}; expected red {g}%.": "原因：{a} → {c}；红色期望 {g}%。",
    "{a} only {g}% ({c}).": "{a} 只有 {g}%（{c}）。",
    "You already have {e}: bonuses of the same type add up, different types multiply.": "你已经有 {e}：同类加成相加，不同类相乘。",
    "memory {a}→{b}": "追忆 {a}→{b}", "red rate {a}%→{b}%": "红色概率 {a}%→{b}%", "replication": "复制",
    "no effect on red": "不影响红色",
    "Why: neither flame raises the expected red.": "原因：两种夜火都不能提高红色期望。",
    "Why: {a} raises the memory count (avg. {m0}→{m1}); {b} raises the red rate with Rarity (avg. {p0}%→{p1}%) but 2 of its 5 kinds (Scrap/Thread ×2) do nothing for red.":
        "原因：{a}提高追忆数量（平均 {m0}→{m1}）；{b}用稀有度提高红色概率（平均 {p0}%→{p1}%），但它 5 种中有 2 种（碎絮/游丝 ×2）对红色没有作用。",
    "Their averages are almost the same.": "两者的平均值几乎相同。",
    "The gains are almost equal; picked the higher-quality {s} flame.": "收益几乎相同；选择了品质更高的{s}夜火。",
    # Void Sea sekmesi (ana pencere)
    "Run {n}  ·  {t}": "第 {n} 次  ·  {t}", "{m} memories  ·  {r} red": "{m} 追忆  ·  {r} 红色",
    "{d}  · now": "{d}  · 进行中",
    "average {a} per run": "平均每次 {a}",
    "+{m}  ({lo}–{hi})": "+{m}  （{lo}–{hi}）",
    "{a} of {b} runs used  ·  {m} Hero Memories  ·  {r} red": "使用 {a}/{b} 次  ·  {m} 英雄追忆  ·  {r} 红色",
    "Left out: run {n} ({t}): {why}": "未计入：第 {n} 次（{t}）：{why}",
    "Model confidence: {c}": "模型可信度：{c}",
    "Model confidence: {c}  —  more runs are needed for reliable comparisons": "模型可信度：{c}  —  需要更多记录才能可靠比较",
    "Red chance: insufficient data ({n} of {k} red needed).": "红色概率：数据不足（需要 {k} 个红色，已有 {n}）。",
    "Combinations: none seen in {n}+ runs yet.": "组合：还没有出现 {n} 次以上的组合。",
    "   {name}:  {a}  ({n} runs, {r} red)": "   {name}：{a}  （{n} 次，{r} 红色）",
    # güncelleme
    "⬆  A new version is available: {tag}  (you have v{v})": "⬆  有新版本：{tag}  （当前 v{v}）",
    "⬆ Update {tag} available — click to open": "⬆ 有更新 {tag} — 点击打开",
    "Version {tag} is available. Open the app and press “Install update”.": "有新版本 {tag}。请打开程序并点击“安装更新”。",
    "⬆  {tag}: {s}": "⬆  {tag}：{s}", "Downloading…": "正在下载…", "Checking…": "正在检查…", "Installing…": "正在安装…",
    "✔  {tag} installed — restarting…": "✔  {tag} 已安装 — 正在重启…",
    "✔  Installed. Restart the app with Start.bat ({e})": "✔  已安装。请用 Start.bat 重新启动程序（{e}）",
    "⚠  Update failed: {e} — use “Release page” to download it yourself.": "⚠  更新失败：{e} — 请使用“发布页面”自行下载。",
    "Download and install {tag} now?\n\nThe app will restart. Your settings and run log (userdata folder) are not touched; the old files are kept in the backup folder.":
        "现在下载并安装 {tag} 吗？\n\n程序会重新启动。你的设置和记录（userdata 文件夹）不会改动；旧文件保存在 backup 文件夹。",
    "A Void Sea run is in progress. Finish the run first, then install the update.": "虚海正在进行中。请先完成这一次，再安装更新。",
    # log yardımı
    "Log file not found. Click “Select log…” and choose UE_game.log in <game folder>\\UE_game\\TorchLight\\Saved\\Logs.":
        "找不到日志文件。请点击“选择日志”，在 <游戏文件夹>\\UE_game\\TorchLight\\Saved\\Logs 中选择 UE_game.log。",
    "To turn the log on in game: log in to a character → Settings → Other → “Enable Log”, then go back to the character select screen and log in again.":
        "在游戏中开启日志：登录角色 → 设置 → 其他 → “开启日志”，然后返回角色选择界面再重新登录。",
    "The game is writing only a basic log, so items, maps and Void Sea runs can't be read. To turn the log on in game: log in to a character → Settings → Other → “Enable Log”, then go back to the character select screen and log in again.":
        "游戏只写入基本日志，因此无法读取物品、地图和虚海记录。在游戏中开启日志：登录角色 → 设置 → 其他 → “开启日志”，然后返回角色选择界面再重新登录。",
    "The game is running but the log is not being written. To turn the log on in game: log in to a character → Settings → Other → “Enable Log”, then go back to the character select screen and log in again.":
        "游戏正在运行，但日志没有写入。在游戏中开启日志：登录角色 → 设置 → 其他 → “开启日志”，然后返回角色选择界面再重新登录。",
}


# {n} sayı, {t} saat/tarih, {x} her şey; diğerleri ";" içermez (yoksa "A; B; C" listesi tek kalıba yutulur)
_SLOT = {"n": r"[\d.,]+", "t": r"[\d:\-. ]+", "x": r".+?"}


def _compile(t):
    parts = re.split(r"\{(\w+)\}", t)
    rx = "".join(re.escape(p) if i % 2 == 0 else f"(?P<{p}>{_SLOT.get(p, '[^;]+?')})" for i, p in enumerate(parts))
    return re.compile(rx, re.S)


_RULES = sorted(((_compile(en), zh) for en, zh in TEMPLATES_ZH.items()), key=lambda r: -len(r[0].pattern))
_GLOSS = sorted(GLOSSARY_ZH.items(), key=lambda kv: -len(kv[0]))


def tr(text):
    """Metni seçili dile çevirir (İngilizcede olduğu gibi döner)."""
    if _LANG["code"] == "en" or not isinstance(text, str) or not text.strip():
        return text
    if "\n" in text and text not in EXACT_ZH:
        return "\n".join(tr(p) for p in text.split("\n"))
    if text in EXACT_ZH:
        return EXACT_ZH[text]
    if text in GLOSSARY_ZH:
        return GLOSSARY_ZH[text]
    for rx, zh in _RULES:
        m = rx.fullmatch(text)
        if m:
            return zh.format(**{k: tr(v) for k, v in m.groupdict().items()})
    core = text.strip()
    if core != text:                                   # baştaki/sondaki boşluk korunur
        return text.replace(core, tr(core), 1)
    if re.search(r"\. \S", text):                      # birden çok cümle (her biri kendi kalıbına uyar)
        parts = re.split(r"(?<=\.) (?=\S)", text)
        if len(parts) > 1:
            return " ".join(tr(p) for p in parts)
    for sep in ("  ·  ", " · ", "; ", ", "):           # birleşik mesaj: parça parça
        if sep in text:
            return sep.join(tr(p) for p in text.split(sep))
    out = text
    for en, zh in _GLOSS:
        out = re.sub(r"(?<![A-Za-z])" + re.escape(en) + r"(?![A-Za-z])", zh, out)
    if out == text and re.search(r"[A-Za-z]{3}", text):
        MISSING.add(text)
    return out
