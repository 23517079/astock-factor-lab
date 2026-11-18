@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo ============================================
echo  刷新因子页面数据
echo  （把最新的评估结果导出给网页「因子体检」页）
echo ============================================
echo.
python export_factor_api.py
if errorlevel 1 (
  echo.
  echo [失败] 导出未成功。常见原因：
  echo   1. 还没跑过 factor_eval.py（缺少 data\factor_eval.json）
  echo   2. Python 环境缺少 pandas / numpy
  echo.
  pause
  exit /b 1
)
echo.
echo 完成。打开或刷新 http://127.0.0.1:8790/factors.html 即可看到最新结果。
echo.
pause
