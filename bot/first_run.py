"""
first_run.py — окно первого запуска агента: что он делает, согласие, автозапуск и ввод кода из бота (Р-16).
"""
import threading

from bot import AGENT_VERSION, device_link
from bot.platform_utils import PROJECTS_DIR, install_autostart

INTRO = (
    "Агент выполняет ваши команды из Telegram-бота Second Brain на этом компьютере: скачивает программы "
    f"с GitHub в папку {PROJECTS_DIR}, запускает их и при необходимости устанавливает Python, Java или Node.js "
    "через winget.\n\n"
    "Команды принимаются только из вашего личного чата с ботом и подписаны ключом этого компьютера. "
    "Задачи, которые ждали, пока компьютер был выключен, выполняются только после вашего подтверждения на экране.\n\n"
    "Отключить компьютер можно в любой момент: в боте «Подключить компьютер» — «Забыть»."
)


def run_first_run_window() -> bool:
    """Показывает окно подключения. True — компьютер подключён, агент можно запускать."""
    import tkinter as tk
    from tkinter import messagebox, ttk

    result = {"paired": False}
    root = tk.Tk()
    root.title("Second Brain — подключение компьютера")
    root.resizable(False, False)
    try:
        root.attributes("-topmost", True)
        root.after(1500, lambda: root.attributes("-topmost", False))
    except tk.TclError:
        pass

    frm = ttk.Frame(root, padding=16)
    frm.grid()
    ttk.Label(frm, text=f"Second Brain Agent {AGENT_VERSION}", font=("Segoe UI", 13, "bold")).grid(sticky="w")
    # Широкий текст — меньше строк: на маленьком экране (ноутбук, Песочница Windows) кнопки иначе уходили за край
    intro = ttk.Label(frm, text=INTRO, wraplength=620, justify="left")
    intro.grid(sticky="w", pady=(8, 8))

    agree = tk.BooleanVar(value=False)
    autostart = tk.BooleanVar(value=True)
    code_var = tk.StringVar()
    name_var = tk.StringVar(value=device_link.default_device_name())

    ttk.Checkbutton(frm, text="Понимаю и разрешаю агенту выполнять мои команды из бота", variable=agree,
                    command=lambda: refresh()).grid(sticky="w")
    ttk.Checkbutton(frm, text="Запускать агент вместе с Windows", variable=autostart).grid(sticky="w", pady=(2, 0))

    fields = ttk.Frame(frm)
    fields.grid(sticky="we", pady=(10, 0))
    ttk.Label(fields, text="Название компьютера (его увидите в боте):").grid(row=0, column=0, sticky="w")
    ttk.Entry(fields, textvariable=name_var, width=30).grid(row=1, column=0, sticky="w", padx=(0, 16))
    ttk.Label(fields, text="Код из бота («Получить код»):").grid(row=0, column=1, sticky="w")
    code_entry = ttk.Entry(fields, textvariable=code_var, width=9, font=("Segoe UI", 14))
    code_entry.grid(row=1, column=1, sticky="w")
    # Кнопки — в одной строке с полем кода, чтобы их было видно вместе с ним
    connect_btn = ttk.Button(fields, text="Подключить", command=lambda: connect())
    connect_btn.grid(row=1, column=2, padx=(10, 0))
    ttk.Button(fields, text="Выйти", command=root.destroy).grid(row=1, column=3, padx=(6, 0))

    status = ttk.Label(frm, text="", foreground="#b00020", wraplength=620, justify="left")
    status.grid(sticky="w", pady=(8, 0))

    busy = {"on": False}

    def refresh() -> None:
        digits = "".join(ch for ch in code_var.get() if ch.isdigit())
        ready = agree.get() and len(digits) == 6 and not busy["on"]
        connect_btn.state(["!disabled"] if ready else ["disabled"])

    code_var.trace_add("write", lambda *_: refresh())

    def finish(error: str) -> None:
        busy["on"] = False
        if error:
            status.configure(text=error, foreground="#b00020")
            refresh()
            return
        note = ""
        if autostart.get() and not install_autostart():
            note = "\n\nАвтозапуск включить не удалось — запускайте SecondBrainAgent.exe вручную после перезагрузки."
        messagebox.showinfo(
            "Second Brain",
            f"Компьютер «{device_link.DEVICE_NAME}» подключён.\n\n"
            "Агент работает в фоне, окно можно не держать открытым. В боте нажмите «Проверить связь»." + note,
            parent=root,
        )
        result["paired"] = True
        root.destroy()

    def connect() -> None:
        if busy["on"]:
            return
        busy["on"] = True
        refresh()
        status.configure(text="Подключаю...", foreground="#444")
        code, name = code_var.get(), name_var.get()

        def work() -> None:
            error = ""
            try:
                device_link.pair(code, name)
            except device_link.PairError as e:
                error = str(e)
            except Exception as e:
                error = f"Не удалось подключить компьютер: {e}"
            root.after(0, lambda: finish(error))

        threading.Thread(target=work, daemon=True).start()

    root.bind("<Return>", lambda _e: connect() if "disabled" not in connect_btn.state() else None)
    refresh()
    # Не выше экрана и ближе к его верху: на маленьком экране сначала уменьшаем текст пояснения
    root.update_idletasks()
    screen_h = root.winfo_screenheight()
    if root.winfo_reqheight() > screen_h - 80:
        intro.configure(font=("Segoe UI", 8))
        root.update_idletasks()
    x = max(0, (root.winfo_screenwidth() - root.winfo_reqwidth()) // 2)
    y = max(0, min((screen_h - root.winfo_reqheight()) // 3, screen_h - root.winfo_reqheight() - 60))
    root.geometry(f"+{x}+{y}")
    code_entry.focus_set()
    root.mainloop()
    return result["paired"]
