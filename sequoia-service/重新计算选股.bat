@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo ==========================================================
echo  重新计算选股（手动触发全链路）
echo ==========================================================
echo.
echo 说明：
echo   · 本脚本调的是 run_factor_pipeline.py（与每日 16:10 定时任务同一条链路）：
echo       增量面板 -^> 缓存追加 -^> 因子时间序列 -^> 滚动验证 -^> 打分 -^> 导出
echo     全链路约 11 分钟；首次准备（初始化 factor_ts）约 4 分钟。
echo   · 「仅近期有效」那一版（recentOnly）已按决策 5 停用，只作为历史断面
echo     原样继承并标记 deprecated。若要现场重算一版对照，用：
echo        重新计算选股.bat /recent
echo.
pause

echo.
echo [1/3] 跑因子全链路（plan -^> compute -^> cache -^> ts -^> rolling -^> score -^> export）
python run_factor_pipeline.py --trigger manual
if errorlevel 1 (
  echo.
  echo [失败] 因子链路未跑通。按退出码对照：
  echo   2 = 校验失败（缓存因子不齐 / 因子时间序列读不到 / 数据层探针未过）
  echo   3 = 已有一次运行持锁
  echo   4 = 连打分都做不出来（没有任何可用因子，本轮无产物）
  echo   5 = 导出/写盘异常
  echo   124 = 超时
  echo   先看 data\factor_run.json 里的 error，那是编排层给的病因。
  pause
  exit /b 1
)

rem 可选：现场重算「仅近期有效」对照版（会覆盖 step7 写出的 recentOnly 段，
rem 但 top / weight / horizon 与链路一致，只是把「继承停用」换成「现场重算」）。
if /i "%~1"=="/recent" (
  echo.
  echo [2/3] 现场重算「仅近期有效」对照版（recentOnly）
  python factor_score.py --recent-filter drop
  if errorlevel 1 (
    echo [失败] 对照版重算失败；主结论（factor_score.json 的 factors/picks）未受影响。
    pause
    exit /b 1
  )
  echo.
  echo [3/3] 重跑导出（让 factor_api.json 跟上刚才的 recentOnly）
  python export_factor_api.py
) else (
  echo.
  echo [2/3] 跳过「仅近期有效」对照版（默认不重算，见文件头说明）
  echo [3/3] 导出已由链路 step8 完成，无需重跑
)

echo.
echo 生成体检报告
python make_factor_report.py

echo.
echo 完成。打开或刷新：
echo   http://127.0.0.1:8790/picks.html     智能选股
echo   http://127.0.0.1:8790/factors.html   因子体检
echo.
echo 提示：页面上的「数据已更新 / 今日待更新 / 已过期 / 更新失败」四态来自
echo   factor_run.json 与行情库的对比，不需要手工改。
echo.
pause
