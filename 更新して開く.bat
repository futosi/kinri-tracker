@echo off
chcp 65001 >nul
rem 住宅ローン金利トラッカー: 最新金利を取得してビューアを開く
cd /d "%~dp0"

echo 最新の金利を取得しています...
python scraper.py
if errorlevel 1 (
  echo.
  echo [!] スクレイパーの実行に失敗しました。
  echo     初回は次を実行してライブラリを入れてください:
  echo         pip install requests beautifulsoup4 lxml
  echo.
  pause
)

echo ビューアを開きます...
start "" "%~dp0index.html"
