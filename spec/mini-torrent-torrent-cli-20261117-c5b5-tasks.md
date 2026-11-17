## Milestone 1: mini-torrent - компактный простой torrent cli клиент

Сгенерировано task_bridge из behaviour-spec бандла mini-torrent-torrent-cli-20261117-c5b5 (шаг 3 плана развития конвейера; группировка задач — по Feature-секциям). Личный путь: бриф одобрен владельцем в разговоре, задачи исполняются сразу.

### TASK-001: Каркас бинаря, пути, конфиг, состояние и IPC
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-03, BEH-04, BEH-19 (DT-01, группа solo).
Source: workstreams/mini-torrent-torrent-cli-20261117-c5b5/spec/30-decomposition.md#DT-01

**Checklist:**
- [x] реализовать BEH-03: При пустом конфиге файлы кладутся в папку по умолчанию
- [x] реализовать BEH-04: Путь из конфига применяется к новым закачкам
- [x] реализовать BEH-19: Один и тот же набор команд отрабатывает на macOS и на Linux
- [x] проверка группы: tests/test_config.py::test_default_download_dir (kind: pytest), tests/test_config.py::test_config_download_dir_applies (kind: pytest), docs/manual/cross-platform.md (kind: manual) зелёные на BEH-03, BEH-04, BEH-19



### TASK-002: Движок закачки: add, параллельность, завершение без раздачи
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-01, BEH-02, BEH-05, BEH-17, BEH-18 (DT-02, группа solo).
Source: workstreams/mini-torrent-torrent-cli-20261117-c5b5/spec/30-decomposition.md#DT-02
**Depends on:** [TASK-001]

**Checklist:**
- [x] реализовать BEH-01: `mt add <magnet>` добавляет закачку и выходит с кодом 0
- [x] реализовать BEH-02: `mt add <файл.torrent>` добавляет закачку из локального файла
- [x] реализовать BEH-05: Три закачки подряд идут одновременно, без очереди
- [x] реализовать BEH-17: На 100% закачка переходит в «готово», соединения закрываются
- [x] реализовать BEH-18: После завершения исходящий трафик по торренту — ноль
- [x] проверка группы: tests/test_engine.py::test_add_magnet_exit_zero_and_listed (kind: pytest), tests/test_engine.py::test_add_torrent_file (kind: pytest), tests/test_engine.py::test_three_adds_all_downloading (kind: pytest), tests/test_engine.py::test_complete_sets_done_and_drops (kind: pytest), tests/test_engine.py::test_no_upload_after_done (kind: pytest) зелёные на BEH-01, BEH-02, BEH-05, BEH-17, BEH-18



### TASK-003: Жизненный цикл демона: самозапуск, автозапуск ОС, возобновление
P2 | 🔍 REVIEW   Est: 0.5d

Реализовать сценарии BEH-07, BEH-08, BEH-09, BEH-10, BEH-11, BEH-20 (DT-03, группа solo).
Source: workstreams/mini-torrent-torrent-cli-20261117-c5b5/spec/30-decomposition.md#DT-03
**Depends on:** [TASK-002]

**Checklist:**
- [x] реализовать BEH-07: Демон живёт после закрытия терминала клиента
- [x] реализовать BEH-08: Демон поднимается сам при первой команде `mt add`
- [x] реализовать BEH-09: После kill демона незавершённые закачки возобновляются сами
- [x] реализовать BEH-10: После перезагрузки машины закачки возобновляются без команды
- [x] реализовать BEH-11: Процент после возобновления не ниже, чем был до остановки
- [x] реализовать BEH-20: Демон переживает отключение ssh-сессии на Linux
- [x] проверка группы: tests/test_daemon_lifecycle.py::test_daemon_survives_client_terminal_close (kind: pytest), tests/test_daemon_lifecycle.py::test_daemon_autospawn_on_first_add (kind: pytest), tests/test_daemon_lifecycle.py::test_resume_after_kill_without_user_command (kind: pytest), docs/manual/reboot-resume.md (kind: manual), tests/test_daemon_lifecycle.py::test_progress_not_lower_after_resume (kind: pytest), docs/manual/ssh-detach.md (kind: manual) зелёные на BEH-07, BEH-08, BEH-09, BEH-10, BEH-11, BEH-20



### TASK-004: Слежение за диском и удаление пропавших записей
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-15, BEH-16 (DT-04, группа solo).
Source: workstreams/mini-torrent-torrent-cli-20261117-c5b5/spec/30-decomposition.md#DT-04
**Depends on:** [TASK-002]

**Checklist:**
- [ ] реализовать BEH-15: Удалённый с диска файл пропадает из состояния и из снапшота
- [ ] реализовать BEH-16: Пропажа записи происходит без перезапуска демона
- [ ] проверка группы: tests/test_watcher.py::test_deleted_file_removed_from_state_and_snapshot (kind: pytest), tests/test_watcher.py::test_removal_on_live_daemon (kind: pytest) зелёные на BEH-15, BEH-16



### TASK-005: TUI со списком закачек и скачанного
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-06, BEH-12, BEH-13, BEH-14 (DT-05, группа solo).
Source: workstreams/mini-torrent-torrent-cli-20261117-c5b5/spec/30-decomposition.md#DT-05
**Depends on:** [TASK-002]

**Checklist:**
- [ ] реализовать BEH-06: Три закачки видны в TUI тремя строками
- [ ] реализовать BEH-12: `mt` и `mt tui` показывают имя, процент и скорость
- [ ] реализовать BEH-13: Уже скачанные файлы видны в том же списке
- [ ] реализовать BEH-14: Прогресс в окне обновляется сам, без нажатий
- [ ] проверка группы: tests/test_tui.py::test_three_rows (kind: pytest), tests/test_tui.py::test_list_columns (kind: pytest), tests/test_tui.py::test_done_entries_listed (kind: pytest), tests/test_tui.py::test_snapshot_refresh_without_input (kind: pytest) зелёные на BEH-06, BEH-12, BEH-13, BEH-14



