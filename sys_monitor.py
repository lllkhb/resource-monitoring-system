# Імпорт бібліотек
import csv
import os
import signal
import time
from datetime import datetime
from typing import Optional

# GPIO необов'язковий: на WSL або ПК без libgpiod скрипт працює без нього
try:
    import gpiod
    from gpiod.line import Direction, Value, LineSettings
    GPIOD_AVAILABLE = True
except ImportError:
    GPIOD_AVAILABLE = False

# Конфігурація
THERMAL_FILE = "/sys/class/thermal/thermal_zone0/temp"
PROC_STAT_FILE = "/proc/stat"
PROC_MEMINFO_FILE = "/proc/meminfo"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(BASE_DIR, "telemetry.csv")
INTERVAL_SEC = 5.0
TEMP_THRESHOLD_C = 60.0

# Приклад для плати, де потрібна лінія відповідає offset 17
GPIO_CHIP = "/dev/gpiochip0"
GPIO_LINE_OFFSET = 17

running = True


# Обробка сигналів
def signal_handler(signum, frame):
    # Запит на коректне завершення роботи
    global running
    print(
        f"[INFO] Отримано сигнал {signum}. Завершення роботи...",
        flush=True,
    )
    running = False


# Керування GPIO
class GPIODController:
    def __init__(self, chip_path: str, line_offset: int):
        self.chip_path = chip_path
        self.line_offset = line_offset
        self.request = None
        self.active = False

        if not GPIOD_AVAILABLE:
            print(
                "[WARN] Бібліотека gpiod недоступна. GPIO вимкнено.",
                flush=True,
            )
            return

        try:
            self.request = gpiod.request_lines(
                chip_path,
                consumer="sys_monitor",
                config={
                    line_offset: LineSettings(
                        direction=Direction.OUTPUT,
                        output_value=Value.INACTIVE,
                    )
                },
            )

            self.active = True

            print(
                f"[INFO] GPIO ініціалізовано: "
                f"{chip_path}, line offset {line_offset}",
                flush=True,
            )

        except (OSError, ValueError, TypeError, AttributeError) as e:
            print(
                f"[ERROR] Не вдалося ініціалізувати GPIO: {e}",
                flush=True,
            )

    def set_state(self, state: bool):
        # Встановлює HIGH або LOW на GPIO
        if not self.active or self.request is None:
            return

        try:
            value = Value.ACTIVE if state else Value.INACTIVE
            self.request.set_value(self.line_offset, value)

        except (OSError, ValueError) as e:
            print(
                f"[ERROR] Помилка запису стану GPIO: {e}",
                flush=True,
            )

    def cleanup(self):
        # Вимикає GPIO та звільняє лінію
        if self.request is None:
            return

        try:
            self.set_state(False)
        finally:
            self.request.release()
            self.request = None
            self.active = False

            print("[INFO] Ресурс GPIO звільнено.", flush=True)


# Збір температури
def read_cpu_temp() -> Optional[float]:
    # Повертає температуру CPU в градусах
    # Якщо датчик недоступний, повертає None
    try:
        with open(THERMAL_FILE, "r", encoding="utf-8") as f:
            temp_raw = float(f.read().strip())

        return round(temp_raw / 1000.0, 2)

    except (FileNotFoundError, PermissionError, ValueError, OSError):
        return None


# Збір даних CPU
def read_cpu_ticks() -> tuple[int, int]:
    # Повертає (idle, total) з першого рядка /proc/stat
    # idle включає idle та iowait
    try:
        with open(PROC_STAT_FILE, "r", encoding="utf-8") as f:
            parts = f.readline().split()

        if not parts or parts[0] != "cpu":
            return 0, 0

        ticks = [int(value) for value in parts[1:]]

        if len(ticks) < 4:
            return 0, 0

        idle_time = ticks[3]
        if len(ticks) > 4:
            idle_time += ticks[4]

        total_time = sum(ticks)

        return idle_time, total_time

    except (OSError, ValueError, IndexError):
        return 0, 0


def calculate_cpu_load(
    prev_idle: int,
    prev_total: int,
    curr_idle: int,
    curr_total: int,
) -> float:
    # Обчислює завантаження CPU у відсотках
    diff_total = curr_total - prev_total
    diff_idle = curr_idle - prev_idle

    if diff_total <= 0:
        return 0.0

    usage = (1.0 - diff_idle / diff_total) * 100.0

    return round(max(0.0, min(100.0, usage)), 2)


# Збір даних RAM
def read_ram_usage() -> float:
    # Обчислює відсоток використаної оперативної пам'яті
    # на основі MemTotal та MemAvailable
    mem_total = 0
    mem_available = None
    mem_free = None

    try:
        with open(PROC_MEMINFO_FILE, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()

                if not parts:
                    continue

                if parts[0] == "MemTotal:":
                    mem_total = int(parts[1])

                elif parts[0] == "MemAvailable:":
                    mem_available = int(parts[1])

                elif parts[0] == "MemFree:":
                    mem_free = int(parts[1])

        if mem_total <= 0:
            return 0.0

        if mem_available is not None:
            available = mem_available
        elif mem_free is not None:
            available = mem_free
        else:
            return 0.0

        available = max(0, min(mem_total, available))

        usage = ((mem_total - available) / mem_total) * 100.0

        return round(usage, 2)

    except (OSError, ValueError, IndexError):
        return 0.0


# Ініціалізація CSV
def init_csv_file(filepath: str):
    # Створює CSV та заголовок, якщо файл відсутній або порожній
    if not os.path.isfile(filepath) or os.path.getsize(filepath) == 0:
        with open(
            filepath,
            mode="w",
            newline="",
            encoding="utf-8",
        ) as f:
            writer = csv.writer(f)
            writer.writerow(
                ["Timestamp", "Temp", "CPU_Load", "RAM_Usage"]
            )

        print(
            f"[INFO] Ініціалізовано лог-файл: {filepath}",
            flush=True,
        )


# Головна функція
def main():
    global running

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print(
        "[INFO] Запуск моніторингу системних ресурсів...",
        flush=True,
    )

    init_csv_file(CSV_FILE)

    gpio = GPIODController(GPIO_CHIP, GPIO_LINE_OFFSET)
    led_state = False

    try:
        if not os.path.exists(THERMAL_FILE):
            print(
                f"[INFO] Датчик {THERMAL_FILE} відсутній. "
                "У CSV записуватиметься N/A.",
                flush=True,
            )

        prev_idle, prev_total = read_cpu_ticks()

        # Початковий інтервал для отримання різниці лічильників CPU
        time.sleep(0.2)

        while running:
            loop_start = time.monotonic()

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Температура CPU
            temp_c = read_cpu_temp()

            if temp_c is not None:
                temp_str = f"{temp_c:.2f}"

                # Керування GPIO за температурним порогом
                if temp_c >= TEMP_THRESHOLD_C:
                    if not led_state:
                        print(
                            f"[ALERT] Перегрів CPU: {temp_c}°C. "
                            "GPIO -> HIGH",
                            flush=True,
                        )

                    gpio.set_state(True)
                    led_state = True

                else:
                    if led_state:
                        print(
                            f"[INFO] Температура нормалізувалася: "
                            f"{temp_c}°C. GPIO -> LOW",
                            flush=True,
                        )

                    gpio.set_state(False)
                    led_state = False

            else:
                temp_str = "N/A"

                if led_state:
                    print(
                        "[INFO] Температура недоступна. "
                        "GPIO -> LOW",
                        flush=True,
                    )

                gpio.set_state(False)
                led_state = False

            # Завантаження CPU
            curr_idle, curr_total = read_cpu_ticks()

            cpu_load = calculate_cpu_load(
                prev_idle,
                prev_total,
                curr_idle,
                curr_total,
            )

            # Оновлюємо попередні лічильники лише за коректного читання
            if curr_total > 0:
                prev_idle, prev_total = curr_idle, curr_total

            # Використання RAM
            ram_usage = read_ram_usage()

            # Запис телеметрії в CSV
            with open(
                CSV_FILE,
                mode="a",
                newline="",
                encoding="utf-8",
            ) as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        timestamp,
                        temp_str,
                        f"{cpu_load:.2f}",
                        f"{ram_usage:.2f}",
                    ]
                )
                f.flush()

            print(
                f"[INFO] Temp={temp_str} °C, "
                f"CPU={cpu_load:.2f}%, RAM={ram_usage:.2f}%",
                flush=True,
            )

            # Очікування до наступного вимірювання
            elapsed = time.monotonic() - loop_start
            sleep_time = max(0.0, INTERVAL_SEC - elapsed)

            deadline = time.monotonic() + sleep_time

            while running:
                remaining = deadline - time.monotonic()

                if remaining <= 0:
                    break

                time.sleep(min(0.2, remaining))

    finally:
        # Виконується і при штатній зупинці, і при помилці циклу
        gpio.cleanup()

        print(
            "[INFO] Моніторинг зупинено.",
            flush=True,
        )


if __name__ == "__main__":
    main()
