"""用量洞察独立板块：导航、图标卡片、动画与可访问性守卫。"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["dashboard.html"])

_dash = io.open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()

print("[insights-page] 独立导航与视图")
check("导航栏有「用量洞察」入口", 'id="btnNavInsights"' in _dash)
check("导航入口带图标（SVG）",
      "btnNavInsights" in _dash and _dash.split('id="btnNavInsights"')[1][:400].count("<svg") == 1)
check("主标签列表包含 insights",
      "'insights'" in _dash.split("const MAIN_TABS =")[1][:200])
check("独立页面 #pageInsights 存在", 'id="pageInsights"' in _dash)
check("切换标签时懒加载洞察数据",
      "if(tab === 'insights') loadInsights();" in _dash)
check("网关页不再内嵌洞察区块",
      'id="insightSection"' not in _dash)

print("[insights-page] 图标卡片与动画")
check("四种图标已定义",
      all(k in _dash for k in ("cost:", "grants:", "days:", "activity:")))
check("卡片渲染抽成 _insightCard",
      "function _insightCard(" in _dash and "insight-icon" in _dash)
check("数字滚动动画（_countUp + rAF）",
      "function _countUp(" in _dash and "requestAnimationFrame(step)" in _dash)
check("尊重 reduced-motion",
      "prefers-reduced-motion" in _dash and "_prefersReduced()" in _dash)
check("进度条宽度过渡", "insight-bar>i" in _dash and "transition:width" in _dash)
check("卡片悬停抬升 + 图标缩放",
      ".insight-card:hover{transform:translateY(-3px)" in _dash
      and ".insight-card:hover .insight-icon" in _dash)
check("表格行悬停与入场 stagger",
      ".insight-table tbody tr:hover" in _dash
      and "insightRowIn" in _dash and "--i" in _dash)
check("刷新按钮有旋转态", "#btnInsightRefresh.spin svg" in _dash)
check("全局按钮/导航过渡",
      ".main-nav-btn:active" in _dash and "button:not(:disabled):active" in _dash)
check("焦点可见样式", ":focus-visible" in _dash)

print("[insights-page] 空状态与提示")
check("空状态组件 _empty", "function _empty(" in _dash)
check("三张表都有空状态文案",
      _dash.count("_empty(") >= 4, _dash.count("_empty("))
check("表头带计数提示（grantsHint/remainingHint/activityHint）",
      all(k in _dash for k in ("grantsHint", "remainingHint", "activityHint")))

finish()