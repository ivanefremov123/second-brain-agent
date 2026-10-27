# Second Brain Agent

Агент для Telegram-бота **Second Brain**: небольшая программа на вашем компьютере, которая по командам из Telegram
скачивает программы с GitHub и запускает их.

* Сам выбирает правильный файл релиза под ваш процессор и систему (x64 / ARM, portable / установщик).
* Распаковывает `.zip`, `.7z`, `.tar.gz` и проверяет, что внутри есть программа.
* Если программе нужна Java, Python, Node.js, Go или Rust — предлагает установить через winget.
* Ничего не выполняет без подписи от облака бота; задачи, ждавшие выключенный компьютер, подтверждаются окном на экране.

## Установка (Windows 10/11)

1. Скачайте `SecondBrainAgent-windows-x64.zip` со страницы [Releases](../../releases/latest).
2. Распакуйте архив в удобную папку (например, `C:\Programs\SecondBrainAgent`).
3. Запустите `SecondBrainAgent.exe`.

### «Система Windows защитила ваш компьютер»

Агент распространяется без платной цифровой подписи, поэтому при первом запуске Windows SmartScreen показывает
предупреждение. Нажмите **«Подробнее»**, затем **«Выполнить в любом случае»**. Исходный код агента открыт в этом
репозитории, а сборка выполняется автоматически GitHub Actions (вкладка Actions) — её можно проверить.

Если антивирус помечает файл как подозрительный: это частое ложное срабатывание на программы, собранные PyInstaller
без подписи. Проверить архив можно на virustotal.com.

## Настройки

Настройки и журналы агента хранятся в `%APPDATA%\SecondBrain`:

* `agent.env` — настройки (образец — `agent.env.example`);
* `logs\agent.log` — журнал работы.

Служебные команды:

```
SecondBrainAgent.exe --selftest            проверка, что агент запускается
SecondBrainAgent.exe --install-autostart   запускать вместе с Windows
SecondBrainAgent.exe --remove-autostart    убрать из автозапуска
```

## Сборка из исходников

```
pip install -r requirements-agent.txt pyinstaller
python tests/test_launch_selection.py
python tests/test_deferred_confirmation.py
pyinstaller --noconfirm packaging/SecondBrainAgent.spec
```
