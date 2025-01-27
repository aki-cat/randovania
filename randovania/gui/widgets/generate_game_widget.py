import dataclasses
import datetime
import logging
import random
import uuid
from functools import partial
from pathlib import Path
from typing import Callable

from PySide6 import QtWidgets, QtGui, QtCore
from qasync import asyncSlot

from randovania.games.game import RandovaniaGame
from randovania.gui.generated.generate_game_widget_ui import Ui_GenerateGameWidget
from randovania.gui.lib import common_qt_lib, async_dialog
from randovania.gui.lib.background_task_mixin import BackgroundTaskMixin
from randovania.gui.lib.generation_failure_handling import GenerationFailureHandler
from randovania.gui.lib.window_manager import WindowManager
from randovania.gui.preset_settings.customize_preset_dialog import CustomizePresetDialog
from randovania.interface_common import simplified_patcher
from randovania.interface_common.options import Options
from randovania.interface_common.preset_editor import PresetEditor
from randovania.layout import preset_describer
from randovania.layout.generator_parameters import GeneratorParameters
from randovania.layout.layout_description import LayoutDescription
from randovania.layout.permalink import Permalink
from randovania.layout.versioned_preset import VersionedPreset, InvalidPreset
from randovania.lib.status_update_lib import ProgressUpdateCallable
from randovania.resolver.exceptions import GenerationFailure


def persist_layout(history_dir: Path, description: LayoutDescription):
    history_dir.mkdir(parents=True, exist_ok=True)

    games = "-".join(sorted(game.short_name for game in description.all_games))

    date_format = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    file_path = history_dir.joinpath(
        f"{date_format}_{games}_{description.shareable_word_hash}.{description.file_extension()}")
    description.save_to_file(file_path)


class PresetMenu(QtWidgets.QMenu):
    action_customize: QtGui.QAction
    action_delete: QtGui.QAction
    action_history: QtGui.QAction
    action_export: QtGui.QAction
    action_duplicate: QtGui.QAction
    action_map_tracker: QtGui.QAction
    action_required_tricks: QtGui.QAction

    action_import: QtGui.QAction
    action_view_deleted: QtGui.QAction

    preset: VersionedPreset | None

    def __init__(self, parent: QtWidgets.QWidget):
        super().__init__(parent)
        self.action_customize = QtGui.QAction(parent)
        self.action_delete = QtGui.QAction(parent)
        self.action_history = QtGui.QAction(parent)
        self.action_export = QtGui.QAction(parent)
        self.action_duplicate = QtGui.QAction(parent)
        self.action_map_tracker = QtGui.QAction(parent)
        self.action_required_tricks = QtGui.QAction(parent)
        self.action_import = QtGui.QAction(parent)
        self.action_view_deleted = QtGui.QAction(parent)

        self.action_customize.setText("Customize")
        self.action_delete.setText("Delete")
        self.action_history.setText("View previous versions")
        self.action_export.setText("Export")
        self.action_duplicate.setText("Duplicate")
        self.action_map_tracker.setText("Open map tracker")
        self.action_required_tricks.setText("View expected trick usage")
        self.action_import.setText("Import")
        self.action_view_deleted.setText("View deleted presets")

        self.addAction(self.action_customize)
        self.addAction(self.action_delete)
        self.addAction(self.action_history)
        self.addAction(self.action_export)
        self.addAction(self.action_duplicate)
        self.addAction(self.action_map_tracker)
        self.addAction(self.action_required_tricks)
        self.addSeparator()
        self.addAction(self.action_import)
        self.addAction(self.action_view_deleted)

        # TODO: Hide the ones that aren't implemented
        self.action_history.setVisible(False)
        self.action_view_deleted.setVisible(False)

    def set_preset(self, preset: VersionedPreset | None):
        self.preset = preset

        for p in [self.action_delete, self.action_history, self.action_export]:
            p.setEnabled(preset is not None and preset.base_preset_uuid is not None)

        for p in [self.action_customize, self.action_duplicate, self.action_map_tracker, self.action_required_tricks]:
            p.setEnabled(preset is not None)


class GenerateGameWidget(QtWidgets.QWidget, Ui_GenerateGameWidget):
    _background_task: BackgroundTaskMixin
    _logic_settings_window: CustomizePresetDialog | None = None
    _has_set_from_last_selected: bool = False
    _preset_menu: PresetMenu
    _action_delete: QtGui.QAction
    _original_show_event: Callable[[QtGui.QShowEvent], None]
    _window_manager: WindowManager
    _options: Options
    game: RandovaniaGame

    def __init__(self):
        super().__init__()
        self.setupUi(self)
        self.failure_handler = GenerationFailureHandler(self)

    def setup_ui(self, game: RandovaniaGame, window_manager: WindowManager, background_task: BackgroundTaskMixin,
                 options: Options):
        self._window_manager = window_manager
        self._background_task = background_task
        self._options = options
        self.game = game

        self.create_preset_tree.game = game
        self.create_preset_tree.window_manager = self._window_manager
        self.create_preset_tree.options = self._options

        # Progress
        self._background_task.background_tasks_button_lock_signal.connect(self.enable_buttons_with_background_tasks)

        self.num_players_spin_box.setVisible(self._window_manager.is_preview_mode)
        self.create_generate_no_retry_button.setVisible(self._window_manager.is_preview_mode)

        # Menu
        self._preset_menu = PresetMenu(self)

        # Signals
        self.create_generate_button.clicked.connect(partial(self.generate_new_layout, True))
        self.create_generate_no_retry_button.clicked.connect(partial(self.generate_new_layout, True, retries=0))
        self.create_generate_race_button.clicked.connect(partial(self.generate_new_layout, False))
        self.create_preset_tree.itemSelectionChanged.connect(self._on_select_preset)
        self.create_preset_tree.customContextMenuRequested.connect(self._on_tree_context_menu)

        self._preset_menu.action_customize.triggered.connect(self._on_customize_preset)
        self._preset_menu.action_delete.triggered.connect(self._on_delete_preset)
        self._preset_menu.action_history.triggered.connect(self._on_view_preset_history)
        self._preset_menu.action_export.triggered.connect(self._on_export_preset)
        self._preset_menu.action_duplicate.triggered.connect(self._on_duplicate_preset)
        self._preset_menu.action_map_tracker.triggered.connect(self._on_open_map_tracker_for_preset)
        self._preset_menu.action_required_tricks.triggered.connect(self._on_open_required_tricks_for_preset)
        self._preset_menu.action_import.triggered.connect(self._on_import_preset)

        self._update_preset_tree_items()

    def _update_preset_tree_items(self):
        self.create_preset_tree.update_items()

    @property
    def _current_preset_data(self) -> VersionedPreset | None:
        return self.create_preset_tree.current_preset_data

    def enable_buttons_with_background_tasks(self, value: bool):
        self.create_generate_button.setEnabled(value)
        self.create_generate_race_button.setEnabled(value)

    def _add_new_preset(self, preset: VersionedPreset):
        with self._options as options:
            options.set_selected_preset_uuid_for(self.game, preset.uuid)

        self._window_manager.preset_manager.add_new_preset(preset)
        self._update_preset_tree_items()
        self.create_preset_tree.select_preset(preset)

    @asyncSlot()
    async def _on_customize_preset(self):
        if self._logic_settings_window is not None:
            self._logic_settings_window.raise_()
            return

        old_preset = self._current_preset_data.get_preset()
        if old_preset.base_preset_uuid is None:
            old_preset = old_preset.fork()

        editor = PresetEditor(old_preset)
        self._logic_settings_window = CustomizePresetDialog(self._window_manager, editor)
        self._logic_settings_window.on_preset_changed(editor.create_custom_preset_with())
        editor.on_changed = lambda: self._logic_settings_window.on_preset_changed(editor.create_custom_preset_with())

        result = await async_dialog.execute_dialog(self._logic_settings_window)
        self._logic_settings_window = None

        if result == QtWidgets.QDialog.Accepted:
            self._add_new_preset(VersionedPreset.with_preset(editor.create_custom_preset_with()))

    def _on_delete_preset(self):
        self._window_manager.preset_manager.delete_preset(self._current_preset_data)
        self._update_preset_tree_items()
        self._on_select_preset()

    def _on_view_preset_history(self):
        pass

    def _on_export_preset(self):
        default_name = f"{self._current_preset_data.slug_name}.rdvpreset"
        path = common_qt_lib.prompt_user_for_preset_file(self._window_manager, new_file=True, name=default_name)
        if path is not None:
            self._current_preset_data.save_to_file(path)

    def _on_duplicate_preset(self):
        old_preset = self._current_preset_data
        self._add_new_preset(VersionedPreset.with_preset(old_preset.get_preset().fork()))

    @asyncSlot()
    async def _on_open_map_tracker_for_preset(self):
        await self._window_manager.open_map_tracker(self._current_preset_data.get_preset())

    def _on_open_required_tricks_for_preset(self):
        from randovania.gui.dialog.trick_usage_popup import TrickUsagePopup
        self._trick_usage_popup = TrickUsagePopup(self, self._window_manager, self._current_preset_data.get_preset())
        self._trick_usage_popup.setWindowModality(QtCore.Qt.WindowModal)
        self._trick_usage_popup.open()

    def _on_import_preset(self):
        path = common_qt_lib.prompt_user_for_preset_file(self._window_manager, new_file=False)
        if path is not None:
            self.import_preset_file(path)

    def import_preset_file(self, path: Path):
        preset = VersionedPreset.from_file_sync(path)
        try:
            preset.get_preset()
        except InvalidPreset:
            QtWidgets.QMessageBox.critical(
                self._window_manager,
                "Error loading preset",
                f"The file at '{path}' contains an invalid preset."
            )
            return

        existing_preset = self._window_manager.preset_manager.preset_for_uuid(preset.uuid)
        if existing_preset is not None:
            user_response = QtWidgets.QMessageBox.warning(
                self._window_manager,
                "Preset ID conflict",
                "The new preset '{}' has the same ID as existing '{}'. Do you want to overwrite it?".format(
                    preset.name,
                    existing_preset.name,
                ),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No | QtWidgets.QMessageBox.Cancel,
                QtWidgets.QMessageBox.Cancel
            )
            if user_response == QtWidgets.QMessageBox.Cancel:
                return
            elif user_response == QtWidgets.QMessageBox.No:
                preset = VersionedPreset.with_preset(dataclasses.replace(preset.get_preset(), uuid=uuid.uuid4()))

        self._add_new_preset(preset)

    def _on_select_preset(self):
        preset_data = self._current_preset_data
        self.on_preset_changed(preset_data)

        if preset_data is not None:
            with self._options as options:
                options.set_selected_preset_uuid_for(self.game, preset_data.uuid)

    def _on_tree_context_menu(self, pos: QtCore.QPoint):
        item: QtWidgets.QTreeWidgetItem = self.create_preset_tree.itemAt(pos)
        preset = None
        if item is not None:
            preset = self.create_preset_tree.preset_for_item(item)

        self._preset_menu.set_preset(preset)
        self._preset_menu.exec_(QtGui.QCursor.pos())

    @property
    def preset(self) -> VersionedPreset:
        preset = self._current_preset_data
        if preset is None:
            preset = self._window_manager.preset_manager.default_preset_for_game(self.game)
        return preset
    
    # Generate seed

    def generate_new_layout(self, spoiler: bool, retries: int | None = None):
        preset = self.preset
        num_players = self.num_players_spin_box.value()

        self.generate_layout_from_permalink(
            permalink=Permalink.from_parameters(GeneratorParameters(
                seed_number=random.randint(0, 2 ** 31),
                spoiler=spoiler,
                presets=[preset.get_preset()] * num_players,
            )),
            retries=retries,
        )

    def generate_layout_from_permalink(self, permalink: Permalink, retries: int | None = None):
        def work(progress_update: ProgressUpdateCallable):
            try:
                layout = simplified_patcher.generate_layout(progress_update=progress_update,
                                                            parameters=permalink.parameters,
                                                            options=self._options,
                                                            retries=retries)
                progress_update(f"Success! (Seed hash: {layout.shareable_hash})", 1)
                persist_layout(self._options.game_history_path, layout)
                self._window_manager.open_game_details(layout)

            except GenerationFailure as generate_exception:
                self.failure_handler.handle_failure(generate_exception)
                progress_update(f"Generation Failure: {generate_exception}", -1)

        if self._window_manager.is_preview_mode:
            logging.info(f"Permalink: {permalink.as_base64_str}")
        self._background_task.run_in_background_thread(work, "Creating a seed...")

    def on_options_changed(self, options: Options):
        if not self._has_set_from_last_selected:
            self._has_set_from_last_selected = True
            preset_manager = self._window_manager.preset_manager
            preset = preset_manager.preset_for_uuid(options.selected_preset_uuid_for(self.game))
            if preset is None:
                preset = preset_manager.default_preset_for_game(self.game)
            self.create_preset_tree.select_preset(preset)

    def on_preset_changed(self, preset: VersionedPreset | None):
        can_generate = False
        if preset is None:
            description = "Please select a preset from the list."

        else:
            try:
                raw_preset = preset.get_preset()
                can_generate = True
                description = f"<p style='font-weight:600;'>{raw_preset.name}</p><p>{raw_preset.description}</p>"
                description += preset_describer.merge_categories(preset_describer.describe(raw_preset))

            except InvalidPreset as e:
                logging.exception(f"Invalid preset for {preset.name}")
                description = (
                    f"Preset {preset.name} can't be used as it contains the following error:"
                    f"\n{e.original_exception}\n"
                    f"\nPlease open edit the preset file with id {preset.uuid} manually or delete this preset."
                )

        self.create_preset_description.setText(description)
        for btn in [self.create_generate_button, self.create_generate_race_button]:
            btn.setEnabled(can_generate)
