import calendar
import concurrent.futures
import copy
import datetime
import itertools
import hashlib
import json
import logging
import multiprocessing
import operator
import os
import re
import random
import sys
import threading
import time
import uuid
from collections import defaultdict
from datetime import date
from math import log
from pathlib import Path
from queue import Queue
from statistics import median

import matplotlib
import requests

matplotlib.use('Qt5Agg')
import matplotlib.pyplot as plt
import numpy as np

import seaborn as sns
from PySide6.QtCore import QPoint, QRect, QSize, QThread, QUrl, Qt, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QDesktopServices, QDoubleValidator, QFont, QFontMetrics, \
    QGuiApplication, \
    QIcon, \
    QIntValidator, \
    QPainter, QPainterPath, \
    QPen, \
    QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication,
    QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout, QLineEdit, QMainWindow,
    QMenu, QMessageBox, QProgressBar, QProgressDialog, QPushButton, QScrollArea, QSizePolicy, QSlider, QSplashScreen,
    QSplitter,
    QStackedLayout,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
    QCheckBox,
)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from qt_material import apply_stylesheet

import asset_utils, graphs, log_parser, stats, updater, version, settings, paths, rearrange, animated_toggle

logging.basicConfig(filename=paths.sbbtracker_folder.joinpath("sbbtracker.log"), filemode="w",
                    format='%(name)s - %(levelname)s - %(message)s', level=logging.WARNING)
logging.getLogger().addHandler(logging.StreamHandler())

from sbbbattlesim import from_state, simulate
from sbbbattlesim.exceptions import SBBBSCrocException
from sbb_window_utils import SBBWindowCheckThread

DEBUG = False

art_dim = (161, 204)
att_loc = (26, 181)
health_loc = (137, 181)
xp_loc = (137, 40)

default_bg_color = "#31363b"
default_bg_color_rgb = "49, 54, 59"
primary_color = "#1de9b6"
sns.set_style("darkgrid", {"axes.facecolor": default_bg_color})

plt.rcParams.update({'text.color': "white",
                     'xtick.color': 'white',
                     'ytick.color': 'white',
                     'figure.facecolor': default_bg_color,
                     'axes.labelcolor': "white"})

round_font = QFont("Roboto", 18)
display_font_family = "Impact" if paths.os_name == "Windows" else "Ubuntu Bold"

patch_notes_file = paths.sbbtracker_folder.joinpath("patch_notes.txt")


def get_image_location(position: int):
    if position < 4:
        x = (161 * position) + 300 + (position * 20)
        y = 0
    elif 4 <= position < 7:
        x = (161 * (position - 4)) + 300 + (161 / 2) + ((position - 4) * 20)
        y = 210
    elif 7 <= position < 9:
        x = (161 * (position - 7))
        y = 440 - 175
    elif position == 9:
        x = (161 / 2)
        y = 440
    elif position == 10:
        x = 850
        y = 440
    elif position == 11:
        x = 1040
        y = 440
    else:
        x = 0
        y = 0
    return x, y + 5


def round_to_xp(round_number: int):
    lvl = min(6, (round_number - 1) // 3 + 2)
    xp = (round_number - 1) % 3 if lvl != 6 else round_number - 13
    return "0.0" if round_number == 0 else f"{lvl}.{xp}"


def update_table(table: QTableWidget, data: list[list]):
    for row in range(len(data)):
        for column in range(len(data[0])):
            datum = (data[row][column])
            table.setItem(row, column, QTableWidgetItem(str(datum)))


all_matches = "All Matches"
latest_patch = "Latest Patch (67.5)"
prev_patch = "Previous Patch (67.4)"
today_ = "Today"
last_7 = "Last 7 days"
last_30 = "Last 30 days"
this_month = "This month"
last_month = "Last month"

default_dates = [all_matches, latest_patch, prev_patch, today_, last_7, last_30, this_month, last_month]


def get_date_range(key):
    today = date.today()
    _, days_this_month = calendar.monthrange(today.year, today.month)
    first_day_this_month = today.replace(day=1)
    last_day_this_month = today.replace(day=days_this_month)
    last_day_prev_month = first_day_this_month - datetime.timedelta(days=1)
    first_day_prev_month = last_day_prev_month.replace(day=1)

    if key == all_matches:
        return "1970-01-01", today.isoformat()
    elif key == latest_patch:
        return "2022-02-07", today.isoformat()
    elif key == prev_patch:
        return "2022-01-24", "2022-02-07"
    elif key == today_:
        return today.isoformat(), today.isoformat()
    elif key == last_7:
        return (today - datetime.timedelta(days=7)).isoformat(), today.isoformat()
    elif key == last_30:
        return (today - datetime.timedelta(days=30)).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")
    elif key == this_month:
        return first_day_this_month.isoformat(), last_day_this_month.isoformat()
    elif key == last_month:
        return first_day_prev_month.isoformat(), last_day_prev_month.isoformat()


api_url = "https://9n2ntsouxb.execute-api.us-east-1.amazonaws.com/prod/api/v1/game"
api_id = settings.get(settings.api_key)
if not api_id:
    api_id = settings.set_(settings.api_key, str(uuid.uuid4()))


def upload_data(payload):
    try:
        resp = requests.post(api_url, data=json.dumps(payload))
        print(resp.content)
    except:
        logging.exception("Unable to post data!")


class SimulationManager(QThread):
    # Maybe have good errors?
    # end_simulation = Signal(str, str, str, str, str)
    # error_simulation = Signal(str)

    def __init__(self, analysis_queue, simulated_stats, results_board):
        super(SimulationManager, self).__init__()
        self.analysis_queue = analysis_queue

        # initialize simulation queue and results
        self.simulated_stats = simulated_stats
        self.results_board = results_board
        self.queue = Queue()

        self.simulation = SimulationThread(self.queue)
        self.simulation.end_simulation.connect(self.update_chances)
        self.simulation.error_simulation.connect(self.sim_error)
        self.simulation.start()

    def sim_error(self, *args, **kwargs):
        self.active_conditions.remove(self.active_condition)

    def update_chances(self, win, tie, loss, win_dmg, loss_dmg):
        self.active_condition.update_reward(win, tie, loss, win_dmg, loss_dmg)
        self.sim_is_done = True

    def eliminate(self):
        mean_win = median([cond.win for cond in self.active_conditions])
        to_remove = []
        for active_condition in self.active_conditions:
            if active_condition.win < mean_win:
                to_remove.append(active_condition)

        for active_condition in to_remove:
            self.active_conditions.remove(active_condition)

    def best_board(self):
        max_win = max(cond.win for cond in self.active_conditions)
        return next(cond for cond in self.active_conditions if cond.win == max_win)

    def stop_condition(self):
        if len(self.active_conditions) == 1:
            return True
        elif len(set(cond.win for cond in self.active_conditions)) == 1:
            self.all_boards_equal = True
            return True
        else:
            return False

    # This should move into the simulator to include supports and treasures
    @staticmethod
    def moves(slot):
        # given a slot, what are the nearest neighbors
        slot_dests = {
            0: (1, 4),
            1: (0, 2, 4, 5),
            2: (1, 3, 5, 6),
            3: (2, 6),
            4: (0, 1, 5),
            5: (1, 2, 4, 6),
            6: (2, 3, 5),
        }[int(slot)]

        print(f"looking to move character in {slot=}")

        return [
            (slot, slot_dest)
            for slot_dest in slot_dests
        ]

    @staticmethod
    def random_slot(board, last_moved_to):
        characters = [
            int(character.slot) for character in board["player"]
            if character.zone == "Character"
            and character.slot != last_moved_to
        ]
        return random.choice(characters)

    @staticmethod
    def hash(board):
        # sort and dump to json
        data = json.dumps(
            dict(
                sorted(
                    (
                        (
                            k,
                            list(
                                map(
                                    lambda x: json.loads(
                                        str(x)),
                                        sorted(v, key=lambda x: (x.slot, x.zone)
                                    )
                                )
                            )
                        )
                        for k, v in board.items()
                    )
                )
            )
        )
        return hashlib.md5(data.encode("utf-8")).hexdigest()


    def run(self):
        num_simulations = 1000 # settings.get(settings.number_threads, 3)
        num_threads = settings.get(settings.number_threads, 3)
        self.all_boards_equal = False
        while True:
            board = self.analysis_queue.get()
            print(board["player"])
            print(list(map(type, board["player"])))
            playerid = "player"
            # search for one local maxima
            current_board = None
            self.simulated_boards = []
            best_boards = []
            for _ in range(3):
                if current_board is None:
                    print("starting search from player board state")
                    current_board = ActiveCondition(board)
                else:
                    print("starting search from random board state")
                    current_board = ActiveCondition(
                        rearrange.randomize_board(board)
                    )
                board_hash = self.hash(current_board.board)
                print(f"{board_hash=}")
                # self.active_condition will concurrently be update with its results
                self.active_condition = current_board
                self.sim_is_done = False
                print(f"running {num_simulations} simulations")
                self.queue.put(
                    # TODO: if ambrosia, error/warn
                    (
                        self.active_condition.board,
                        playerid,
                        num_simulations,
                        num_threads,
                    )
                )
                while not self.sim_is_done:
                    time.sleep(1)

                self.results_board.composition = current_board.board["player"]
                self.results_board.update()
                self.simulated_stats.update_chances(
                    *current_board.chances(),
                )
                print(f"Initial result was:\n  {self.active_condition.chances()}")
                self.simulated_boards.append(board_hash)
                # if all options get worse/stay the same, break
                best_boards.append(self.active_condition)
                last_res = self.active_condition.win

                last_moved_to = None
                for _ in range(7):
                    random_restart = True
                    while True:
                        if random_restart is True:
                            # start at a random slot
                            moves = self.moves(
                                self.random_slot(current_board.board, last_moved_to)
                            )
                            random_restart = False
                        else:
                            # keep climbing the hill if we just made a positive step
                            moves = self.moves(last_moved_to)
                        step_results = []
                        for move in moves:
                            new_board = rearrange.make_swap(
                                current_board.board, *move
                            )

                            # should make this an @cachedproperty of ActiveConditon
                            board_hash = self.hash(new_board)
                            print(f"{board_hash=}")
                            if board_hash in self.simulated_boards:
                                continue
                            self.active_condition = ActiveCondition(new_board)
                            self.active_condition.move = move
                            # self.active_condition will concurrently be update with its results
                            step_results.append(self.active_condition)
                            self.sim_is_done = False
                            print(f"running {num_simulations} simulations")
                            self.queue.put(
                                # TODO: if ambrosia, error/warn
                                (
                                    self.active_condition.board,
                                    playerid,
                                    num_simulations,
                                    num_threads,
                                )
                            )
                            while not self.sim_is_done:
                                time.sleep(1)

                            self.simulated_boards.append(board_hash)

                        if not step_results:
                            print("all neighbors previously simulated")
                            break

                        max_res = max(map(lambda condition: condition.win, step_results))
                        best_step = next(
                            condition
                            for condition in step_results
                            if condition.win == max_res
                        )
                        # if all options get worse/stay the same, do a random restart
                        if max_res <= last_res:
                            print("No step yields better result")
                            best_boards.append(current_board)
                            break
                        print(f"best result was: {best_step.move=}\n  {best_step.chances()}")
                        current_board = best_step
                        last_moved_to = best_step.move[-1]
                        last_res = max_res

                        self.results_board.composition = current_board.board["player"]
                        self.results_board.update()
                        self.simulated_stats.update_chances(
                            *current_board.chances(),
                        )

            best_result = max(map(lambda condition: condition.win, best_boards))
            best_board = next(
                condition
                for condition in best_boards
                if condition.win == best_result
            )
            # move is (from, to)
            self.results_board.composition = best_board.board["player"]
            self.results_board.update()
            self.simulated_stats.update_chances(
                *best_board.chances(),
            )
            print("SIMULATION DONE")


class SimulationThread(QThread):
    end_simulation = Signal(str, str, str, str, str)
    error_simulation = Signal(str)

    def __init__(self, comp_queue):
        super(SimulationThread, self).__init__()
        self.comp_queue = comp_queue

    def run(self):
        while True:
            board, playerid, num_simulations, num_threads = self.comp_queue.get()
            simulation_stats = None

            if playerid is None:
                playerid = settings.get(settings.player_id)
            simulator_board = asset_utils.replace_template_ids(board)
            from_stated = from_state(simulator_board)
            if all([from_stated[player_id]['level'] != 0 for player_id in from_stated]):
                try:
                    simulation_stats = simulate(simulator_board, t=num_threads, k=int(num_simulations / num_threads),
                                                timeout=30)
                except SBBBSCrocException:
                    self.error_simulation.emit("Captain Croc not supported")
                except concurrent.futures.TimeoutError:
                    self.error_simulation.emit("Simulation timed out!")
                except Exception:
                    logging.exception("Error in simulation!")
                    with open(stats.sbbtracker_folder.joinpath("error_board.json"), "w") as file:
                        json.dump(from_stated, file, default=lambda o: o.__dict__)
                    self.error_simulation.emit("Error in simulation!")

                logging.debug(from_stated)

                # TODO: do math on changes in XP
                if simulation_stats:
                    results = simulation_stats.results
                    aggregated_results = defaultdict(list)
                    for result in results:
                        aggregated_results[result.win_id].append(result.damage)

                    keys = set(aggregated_results.keys()) - {playerid, None}
                    win_damages = aggregated_results.get(playerid, [])
                    tie_damages = aggregated_results.get(None, [])
                    loss_damages = [] if not keys else aggregated_results[keys.pop()]

                    win_percent = round(len(win_damages) / len(results) * 100, 2)
                    tie_percent = round(len(tie_damages) / len(results) * 100, 2)
                    loss_percent = round(len(loss_damages) / len(results) * 100, 2)
                    # win_10th_percentile, win_90th_percentile = (0, 0) if (len(win_damages) == 0) \
                    #     else np.percentile(win_damages, [10, 90])
                    # loss_10th_percentile, loss_90th_percentile = (0, 0) if (len(loss_damages) == 0) \
                    #     else np.percentile(loss_damages, [10, 90])

                    win_string = str(win_percent) + "%"
                    tie_string = str(tie_percent) + "%"
                    loss_string = str(loss_percent) + "%"
                    win_dmg_string = str(round(np.mean(win_damages), 1) if win_percent > 0 else 0)
                    loss_dmg_string = str(round(np.mean(loss_damages), 1) if loss_percent > 0 else 0)

                    self.end_simulation.emit(win_string, tie_string, loss_string, win_dmg_string, loss_dmg_string)
            else:
                self.error_simulation.emit("Couldn't get player id (try reattaching)")
            time.sleep(1)


class LogThread(QThread):
    round_update = Signal(int)
    player_update = Signal(object, int)
    comp_update = Signal(object, int)
    stats_update = Signal(str, object, str, object)
    player_info_update = Signal(graphs.LivePlayerStates)
    health_update = Signal(object)
    new_game = Signal(bool)
    end_combat = Signal()

    def run(self):
        queue = Queue()
        threading.Thread(target=log_parser.run,
                         args=(
                             queue,),
                         daemon=True).start()
        round_number = 0
        current_player = None
        counter = 0
        states = graphs.LivePlayerStates()
        matchmaking = False
        after_first_combat = False
        session_id = None
        build_id = None
        match_data = {}
        combats = []
        while True:
            update = queue.get()
            job = update.job
            state = update.state
            if job == log_parser.JOB_MATCHMAKING:
                matchmaking = True
            elif job == log_parser.JOB_NEWGAME:
                states.clear()
                match_data.clear()
                current_player = None
                round_number = 0
                self.new_game.emit(matchmaking)
                self.round_update.emit(0)
                matchmaking = False
                after_first_combat = False
                session_id = state.session_id
                build_id = state.build_id
                combats.clear()
                match_data.clear()
            elif job == log_parser.JOB_INITCURRENTPLAYER:
                if not after_first_combat:
                    current_player = state
                    settings.get(settings.player_id, state.playerid)
                    # only save the first time
                self.player_update.emit(state, round_number)
            elif job == log_parser.JOB_ROUNDINFO:
                round_number = state.round_num
                self.round_update.emit(round_number)
            elif job == log_parser.JOB_PLAYERINFO:
                self.player_update.emit(state, round_number)
                xp = f"{state.level}.{state.experience}"
                states.update_player(state.playerid, round_number, state.health, xp,
                                     asset_utils.get_card_name(state.heroid), state.heroid)
                counter += 1
                if counter == 8:
                    self.player_info_update.emit(states)
                    if after_first_combat:
                        self.end_combat.emit()
                if not after_first_combat:
                    after_first_combat = True
            elif job == log_parser.JOB_BOARDINFO:
                self.comp_update.emit(state, round_number)

                combat = from_state(state)
                combat["round"] = round_number
                combats.append(combat)
            elif job == log_parser.JOB_ENDCOMBAT:
                counter = 0
            elif job == log_parser.JOB_ENDGAME:
                self.end_combat.emit()
                if state and current_player and session_id and build_id:
                    match_data["tracker-id"] = api_id
                    match_data["tracker-version"] = version.__version__
                    match_data["player-id"] = current_player.playerid
                    match_data["display-name"] = current_player.displayname
                    match_data["match-id"] = session_id
                    match_data["build-id"] = build_id
                    match_data["combat-info"] = combats
                    match_data["placement"] = state.place
                    match_data["players"] = states.json_friendly()
                    self.stats_update.emit(asset_utils.get_card_name(current_player.heroid), state, session_id, match_data)
            elif job == log_parser.JOB_HEALTHUPDATE:
                self.health_update.emit(state)


class ImportThread(QThread):
    update_progress = Signal(int, int)

    def __init__(self, player_stats: stats.PlayerStats):
        super(ImportThread, self).__init__()
        self.player_stats = player_stats

    def run(self):
        self.player_stats.import_matches(self.update_progress.emit)


class SliderCombo(QWidget):
    def __init__(self, minimum, maximum, default, step=1):
        super().__init__()
        slider_editor = QHBoxLayout(self)
        self.slider = QSlider(Qt.Horizontal)
        self.editor = QLineEdit()
        self.slider.setMaximum(maximum)
        self.slider.setMinimum(minimum)
        self.slider.setValue(default)
        self.slider.setSingleStep(step)
        self.slider.setTickInterval(step)
        self.slider.setMinimumWidth(100)
        self.slider.valueChanged.connect(lambda val: self.editor.setText(str(val)))
        self.editor.setValidator(QIntValidator(0, maximum))
        self.editor.setText(str(default))
        self.editor.textEdited.connect(
            lambda text: self.slider.setValue(int(text)) if text != '' else None)
        self.editor.setMinimumWidth(100)
        slider_editor.addWidget(self.slider)
        slider_editor.addWidget(self.editor)

    def get_value(self):
        return int(self.editor.text()) if self.editor.text() else 0


class HexColorEdit(QWidget):
    def __init__(self, default):
        super().__init__()
        layout = QHBoxLayout(self)
        self.editor = QLineEdit()
        color_box = QWidget()
        color_box.setMinimumSize(40, 40)
        layout.addWidget(self.editor)
        layout.addWidget(color_box)

        self.editor.textEdited.connect(lambda text: self.update_color(text, color_box))
        self.editor.setText(default)

    def update_color(self, text: str, widget: QWidget):
        valid_hex_color = re.search(r'^#(?:[0-9a-fA-F]{3}){1,2}$', text)

        if valid_hex_color:
            widget.setStyleSheet(f"background-color: {text} ;")


class SettingsCheckbox(QCheckBox):
    def __init__(self, setting: settings.Setting):
        super().__init__()
        self.setChecked(settings.get(setting))
        self.stateChanged.connect(lambda: settings.toggle(setting))


class SettingsWindow(QMainWindow):
    def __init__(self, main_window):
        super().__init__()
        self.hide()
        self.setWindowModality(Qt.ApplicationModal)
        self.main_window = main_window
        main_widget = QFrame()
        main_layout = QVBoxLayout(main_widget)
        general_settings = QWidget()
        overlay_settings = QWidget()
        overlay_settings_scroll = QScrollArea(widgetResizable=True)
        overlay_settings_scroll.setWidget(overlay_settings)
        about_tab = QWidget()
        data_tab = QWidget()
        advanced_tab = QWidget()
        streaming_tab = QWidget()
        settings_tabs = QTabWidget()
        settings_tabs.addTab(general_settings, "General")
        settings_tabs.addTab(data_tab, "Data")
        settings_tabs.addTab(overlay_settings_scroll, "Overlay")
        settings_tabs.addTab(advanced_tab, "Advanced")
        settings_tabs.addTab(streaming_tab, "Streaming")
        settings_tabs.addTab(about_tab, "About")

        self.setWindowIcon(QIcon(asset_utils.get_asset("icon.png")))
        self.setWindowTitle("SBBTracker settings")

        about_layout = QVBoxLayout(about_tab)
        about_layout.addWidget(QLabel(f"""SBBTracker v{version.__version__}


SBBBattleSim by:
reggles44
isik
fredyybob


Special thanks to:
NoLucksGiven,
Asado,
HamiO,
chickenArise,
bnor,
and Lunco
"""))
        about_layout.addStretch()

        general_layout = QFormLayout(general_settings)

        save_stats_checkbox = SettingsCheckbox(settings.save_stats)

        self.graph_color_chooser = QComboBox()
        palettes = list(graphs.color_palettes.keys())
        self.graph_color_chooser.addItems(palettes)
        self.graph_color_chooser.setCurrentIndex(palettes.index(settings.get(settings.live_palette)))
        self.graph_color_chooser.currentTextChanged.connect(main_window.live_graphs.set_color_palette)

        matchmaking_only_checkbox = SettingsCheckbox(settings.matchmaking_only)
        matchmaking_only_checkbox.setEnabled(bool(save_stats_checkbox.checkState()))

        save_stats_checkbox.stateChanged.connect(lambda state: matchmaking_only_checkbox.setEnabled(bool(state)))

        general_layout.addRow("Save match results", save_stats_checkbox)
        general_layout.addRow("Ignore practice and group lobbies", matchmaking_only_checkbox)
        general_layout.addRow("Graph color palette", self.graph_color_chooser)

        data_layout = QFormLayout(data_tab)
        export_button = QPushButton("Export Stats")
        export_button.clicked.connect(main_window.export_csv)
        delete_button = QPushButton("Delete Stats")
        delete_button.clicked.connect(lambda: main_window.delete_stats(self))
        self.last_backed_up = QLabel(f"Last backed up on: {stats.most_recent_backup_date()}")
        backup_button = QPushButton("Backup Stats")
        backup_button.clicked.connect(self.backup)
        reimport_button = QPushButton("Reimport Stats")
        reimport_button.clicked.connect(self.import_stats)

        enable_upload = SettingsCheckbox(settings.upload_data)

        data_layout.addRow(self.last_backed_up, backup_button)
        data_layout.addWidget(export_button)
        data_layout.addWidget(delete_button)
        data_layout.addWidget(reimport_button)
        data_layout.addRow("Upload matches to sbbtracker.com", enable_upload)
        data_layout.addWidget(QLabel("Match uploads include your steam name, sbb id, board comps,"
                                     "placement, and change in mmr."))

        overlay_layout = QFormLayout(overlay_settings)
        enable_overlay_checkbox = SettingsCheckbox(settings.enable_overlay)

        hide_overlay_in_bg_checkbox = SettingsCheckbox(settings.hide_overlay_in_bg)

        enable_overlay_checkbox.stateChanged.connect(lambda state: hide_overlay_in_bg_checkbox.setEnabled(bool(state)))

        enable_sim_checkbox = SettingsCheckbox(settings.enable_sim)
        enable_sim_checkbox.setEnabled(enable_overlay_checkbox.checkState())

        enable_overlay_checkbox.stateChanged.connect(lambda state: enable_sim_checkbox.setEnabled(bool(state)))

        show_tracker_button_checkbox = SettingsCheckbox(settings.show_tracker_button)
        show_tracker_button_checkbox.setEnabled(enable_overlay_checkbox.checkState())

        enable_overlay_checkbox.stateChanged.connect(lambda state: show_tracker_button_checkbox.setEnabled(bool(state)))

        enable_comps = SettingsCheckbox(settings.enable_comps)
        enable_comps.setEnabled(enable_overlay_checkbox.checkState())

        enable_overlay_checkbox.stateChanged.connect(lambda state: enable_comps.setEnabled(bool(state)))

        enable_turn_display = SettingsCheckbox(settings.enable_turn_display)
        enable_turn_display.setEnabled(enable_overlay_checkbox.checkState())

        enable_overlay_checkbox.stateChanged.connect(lambda state: enable_turn_display.setEnabled(bool(state)))

        turn_display_font = QLineEdit()
        turn_display_font.setValidator(QIntValidator(1, 100))
        turn_display_font.setText(str(settings.get(settings.turn_display_font_size)))
        turn_display_font.textChanged.connect(
            lambda text: settings.set_(settings.turn_display_font_size, text) if text != '' else None)

        windows_scaling = SettingsCheckbox(settings.disable_scaling)
        windows_scaling.setEnabled(enable_overlay_checkbox.checkState())

        enable_overlay_checkbox.stateChanged.connect(lambda state: windows_scaling.setEnabled(bool(state)))

        self.comp_transparency_slider = SliderCombo(0, 100, settings.get(settings.boardcomp_transparency))
        self.simulator_transparency_slider = SliderCombo(0, 100, settings.get(settings.simulator_transparency))
        self.num_sims_silder = SliderCombo(100, 10000, settings.get(settings.number_simulations, 1000))
        self.num_threads_slider = SliderCombo(1, 4, settings.get(settings.number_threads))
        self.overlay_comps_scaling = SliderCombo(50, 200, settings.get(settings.overlay_comps_scaling))

        overlay_layout.addRow("Enable overlay (does not work in fullscreen)", enable_overlay_checkbox)
        overlay_layout.addRow("Hide if SBB in background (restart to take effect)", hide_overlay_in_bg_checkbox)
        overlay_layout.addRow(QLabel(" "))
        overlay_layout.addRow("Enable simulator", enable_sim_checkbox)
        overlay_layout.addRow("Number of simulations", self.num_sims_silder)
        overlay_layout.addRow("Number of threads", self.num_threads_slider)
        overlay_layout.addRow(QLabel("More threads = faster simulation but takes more computing power"))
        overlay_layout.addRow(QLabel(" "))
        overlay_layout.addRow("Enable \"Show Tracker\" button", show_tracker_button_checkbox)
        overlay_layout.addRow("Enable board comps", enable_comps)
        overlay_layout.addRow("Board comps scaling", self.overlay_comps_scaling)
        overlay_layout.addRow("Enable turn display", enable_turn_display)
        overlay_layout.addRow("Turn font size (restart to resize)", turn_display_font)
        overlay_layout.addRow("Ignore windows scaling (Windows 8 compat)", windows_scaling)
        overlay_layout.addRow("Adjust comps transparency", self.comp_transparency_slider)
        overlay_layout.addRow("Adjust simulator transparency", self.simulator_transparency_slider)

        advanced_layout = QFormLayout(advanced_tab)
        enable_export_comp_checkbox = SettingsCheckbox(settings.export_comp_button)
        show_id_mode = SettingsCheckbox(settings.show_ids)
        advanced_layout.addRow("Enable export last comp button", enable_export_comp_checkbox)
        advanced_layout.addRow("Hide art and show template ids", show_id_mode)

        streaming_layout = QFormLayout(streaming_tab)
        enable_stream_overlay = QCheckBox()
        enable_stream_overlay.setChecked(settings.get(settings.streaming_mode))
        enable_stream_overlay.stateChanged.connect(lambda: settings.toggle(settings.streaming_mode))

        self.stream_overlay_color = HexColorEdit(settings.get(settings.stream_overlay_color))

        streamable_score = QCheckBox()
        streamable_score.setChecked(settings.get(settings.streamable_score_list))
        streamable_score.stateChanged.connect(lambda: settings.toggle(settings.streamable_score_list))

        self.max_scores = QLineEdit()
        self.max_scores.setText(str(settings.get(settings.streamable_score_max_len)))
        self.max_scores.setValidator(QIntValidator(1, 50))

        reset_scores = QPushButton("Reset")
        reset_scores.clicked.connect(main_window.streamable_scores.reset)

        streaming_layout.addRow("Show capturable score window", streamable_score)
        streaming_layout.addRow("Number of scores per line", self.max_scores)
        streaming_layout.addRow("Reset scores", reset_scores)
        streaming_layout.addRow(QLabel("Chroma-key filter #00FFFF to hide the scores background"))
        streaming_layout.addRow(QLabel(""))
        streaming_layout.addRow("Show capturable overlay window", enable_stream_overlay)
        streaming_layout.addRow("Background color", self.stream_overlay_color)
        streaming_layout.addRow(QLabel("Enabling this will add a copy of the overlay behind your other windows."))
        streaming_wiki_link = QLabel(
            "<a href=\"https://github.com/SBBTracker/SBBTracker/wiki/Streaming-Settings-Guide\" style=\"color: "
            f"{primary_color};\">Wiki guide here</a>")
        streaming_wiki_link.setTextFormat(Qt.RichText)
        streaming_wiki_link.setTextInteractionFlags(Qt.TextBrowserInteraction)
        streaming_wiki_link.setOpenExternalLinks(True)
        streaming_layout.addRow(streaming_wiki_link)

        save_close_layout = QHBoxLayout()
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self.save)
        close_button = QPushButton("Cancel")
        close_button.clicked.connect(self.hide)
        save_close_layout.addStretch()
        save_close_layout.addWidget(self.save_button)
        save_close_layout.addWidget(close_button)

        main_layout.addWidget(settings_tabs)
        main_layout.addLayout(save_close_layout)

        self.setCentralWidget(main_widget)
        self.setMinimumSize(600, 600)

    def save(self):
        settings.set_(settings.live_palette, self.graph_color_chooser.currentText())
        settings.set_(settings.boardcomp_transparency, self.comp_transparency_slider.get_value())
        settings.set_(settings.simulator_transparency, self.simulator_transparency_slider.get_value())
        settings.set_(settings.number_threads, self.num_threads_slider.get_value())
        settings.set_(settings.number_simulations, self.num_sims_silder.get_value())
        settings.set_(settings.stream_overlay_color, self.stream_overlay_color.editor.text())
        settings.set_(settings.overlay_comps_scaling, self.overlay_comps_scaling.get_value())

        max_scores_val = self.max_scores.text()
        if max_scores_val and int(max_scores_val) > 0:
            settings.set_(settings.streamable_score_max_len, int(max_scores_val))

        settings.save()
        self.hide()

        # self.main_window.overlay.update_monitor()
        self.main_window.overlay.update_comp_scaling()
        self.main_window.streamer_overlay.update_comp_scaling()
        self.main_window.overlay.set_transparency()
        self.main_window.show_scores()
        if not settings.get(settings.hide_overlay_in_bg) or self.main_window.overlay.visible:
            self.main_window.show_overlay()
        if settings.get(settings.streaming_mode):
            self.main_window.streamer_overlay.show()
            self.main_window.streamer_overlay.centralWidget().setStyleSheet(
                f"QWidget#overlay {{background-color: {settings.get(settings.stream_overlay_color)}}}")
            self.main_window.streamer_overlay.turn_display.setVisible(settings.get(settings.enable_turn_display))
        else:
            self.main_window.streamer_overlay.hide()
        self.main_window.overlay.set_comps_enabled(settings.get(settings.enable_comps))
        self.main_window.streamer_overlay.set_comps_enabled(settings.get(settings.enable_comps))
        self.main_window.overlay.simulation_stats.setVisible(settings.get(settings.enable_sim))
        self.main_window.overlay.show_button.setVisible(settings.get(settings.show_tracker_button))
        self.main_window.overlay.turn_display.setVisible(settings.get(settings.enable_turn_display))
        self.main_window.export_comp_action.setVisible(settings.get(settings.export_comp_button))

    def import_stats(self):
        message = """
Would you like to import your old games? This is done by
reading the record files generated by the game. This will
import games going back to Dec 14 2021 (assuming you have not
deleted the record files).

Note that not all games are guaranteed to be imported.

If you have games already recorded from before Jan 2nd 2022
duplicated matches may appear. You may wish to backup and delete
your current stats before doing this, or manually remove stats
yourself.

The importer may take a long time to complete. Please be patient.



"""
        reply = QMessageBox.question(self, "Reimport Stats?", message)
        if reply == QMessageBox.Yes:
            self.import_thread = ImportThread(self.main_window.player_stats)
            self.progress = QProgressDialog("Import progress", "Cancel", 0, 100, self)
            self.progress.setWindowTitle("Importer")
            self.import_thread.update_progress.connect(self.handle_import_progress)
            self.import_thread.start()
            self.progress.canceled.connect(self.import_thread.terminate)
            self.progress.show()
            self.main_window.match_history.update_history_table()

    def backup(self):
        stats.backup_stats(force=True)
        self.last_backed_up.setText(f"Last backed up on: {stats.most_recent_backup_date()}")

    def handle_import_progress(self, num, totalsize):
        import_percent = num * 100 / totalsize
        self.progress.setValue(import_percent)
        if num == totalsize:
            self.progress.close()


class SBBTracker(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("SBBTracker")
        self.comps = [BoardComp() for _ in range(0, 8)]
        self.round_indicator = QLabel("Waiting for match to start...")
        self.round_indicator.setFont(round_font)
        self.player_stats = stats.PlayerStats()
        self.player_ids = []
        self.most_recent_combat = None
        self.in_matchmaking = False

        self.overlay = OverlayWindow(self)
        self.streamer_overlay = StreamerOverlayWindow(self)
        self.overlay.stream_overlay = self.streamer_overlay
        settings.get(settings.enable_overlay)
        self.show_overlay()
        if settings.get(settings.streaming_mode):
            self.streamer_overlay.show()
        self.streamable_scores = StreamableMatchDisplay()
        self.show_scores()

        self.comp_tabs = QTabWidget()
        for index in range(len(self.comps)):
            self.comp_tabs.addTab(self.comps[index], f"Player{index}")

        self.reset_button = QPushButton("Reattach to Storybook Brawl")
        self.reset_button.setMaximumWidth(self.reset_button.fontMetrics().boundingRect("Reattach to Storybook Brawl")
                                          .width() * 2)
        self.reset_button.clicked.connect(self.reattach_to_log)
        round_widget = QWidget()
        round_layout = QHBoxLayout(round_widget)
        round_layout.addWidget(self.round_indicator)
        round_layout.addWidget(self.reset_button)

        comps_widget = QWidget()
        layout = QVBoxLayout(comps_widget)
        layout.addWidget(round_widget)
        layout.addWidget(self.comp_tabs)

        self.match_history = MatchHistory(self, self.player_stats)
        self.live_graphs = LiveGraphs()
        self.stats_graph = StatsGraph(self.player_stats)
        self.board_analysis = BoardAnalysis(size=self.size(), player_ids=self.player_ids)

        main_tabs = QTabWidget()
        main_tabs.addTab(comps_widget, "Board Comps")
        main_tabs.addTab(self.live_graphs, "Live Graphs")
        main_tabs.addTab(self.match_history, "Match History")
        main_tabs.addTab(self.stats_graph, "Stats Graphs")
        main_tabs.addTab(self.board_analysis, "Board Analysis")

        toolbar = QToolBar(self)
        toolbar.setMinimumHeight(40)
        toolbar.setStyleSheet("QToolBar {border-bottom: none; border-top: none;}")
        discord_action = QAction(QPixmap(asset_utils.get_asset("icons/discord.png")), "&Join our Discord", self)
        # toolbar.insertAction(toolbar.minimize, discord_action)
        toolbar.addAction(discord_action)
        discord_action.triggered.connect(self.open_discord)

        bug_action = QAction(QPixmap(asset_utils.get_asset("icons/bug_report.png")), "&Report a bug", self)
        toolbar.insertAction(discord_action, bug_action)
        bug_action.triggered.connect(self.open_issues)

        self.settings_window = SettingsWindow(self)
        settings_action = QAction(QPixmap(asset_utils.get_asset("icons/settings.png")), "&Settings", self)
        toolbar.insertAction(bug_action, settings_action)
        settings_action.triggered.connect(self.settings_window.show)

        self.export_comp_action = QAction(QPixmap(asset_utils.get_asset("icons/file-export.png")),
                                          "&Export last combat", self)
        toolbar.insertAction(bug_action, self.export_comp_action)
        self.export_comp_action.triggered.connect(self.export_last_comp)
        self.export_comp_action.setVisible(settings.get(settings.export_comp_button))

        patch_notes_action = QAction(QPixmap(asset_utils.get_asset("icons/information.png")), "&Patch Notes", self)
        toolbar.insertAction(self.export_comp_action, patch_notes_action)
        patch_notes_action.triggered.connect(self.show_patch_notes)

        main_tabs.setCornerWidget(toolbar)

        self.update_banner = QToolBar(self)
        self.update_banner.setMinimumHeight(40)
        self.update_banner.setStyleSheet(
            f"QToolBar {{border-bottom: none; border-top: none; background: {primary_color};}}")
        update_text = QLabel("    An update is available! Would you like to install?    ")
        update_text.setStyleSheet(f"QLabel {{ color : {default_bg_color}; }}")
        self.update_banner.addWidget(update_text)

        yes_update = QAction("&Yes", self)
        yes_update.triggered.connect(self.install_update)
        no_update = QAction("&Remind me later", self)
        no_update.triggered.connect(self.update_banner.hide)
        self.update_banner.addAction(yes_update)
        self.update_banner.addAction(no_update)
        self.update_banner.hide()

        self.setWindowIcon(QIcon(asset_utils.get_asset("icon.png")))

        main_widget = QWidget()
        main_layout = QVBoxLayout(main_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(self.update_banner)
        main_layout.addWidget(main_tabs)

        self.setCentralWidget(main_widget)
        self.setMinimumSize(QSize(1200, 800))
        self.setBaseSize(QSize(1400, 900))
        self.github_updates = updater.UpdateCheckThread()
        self.github_updates.github_update.connect(self.handle_update)

        self.log_updates = LogThread()
        self.log_updates.comp_update.connect(self.update_comp)
        self.log_updates.player_update.connect(self.update_player)
        self.log_updates.round_update.connect(self.update_round_num)
        self.log_updates.stats_update.connect(self.update_stats)
        self.log_updates.player_info_update.connect(self.live_graphs.update_graph)
        self.log_updates.player_info_update.connect(self.overlay.update_placements)
        self.log_updates.player_info_update.connect(self.streamer_overlay.update_placements)
        self.log_updates.new_game.connect(self.new_game)
        self.log_updates.health_update.connect(self.update_health)
        self.log_updates.end_combat.connect(self.end_combat)

        self.board_queue = Queue()
        self.simulation = SimulationThread(self.board_queue)
        self.simulation.end_simulation.connect(self.overlay.simulation_stats.update_chances)
        self.simulation.end_simulation.connect(self.streamer_overlay.simulation_stats.update_chances)
        self.simulation.error_simulation.connect(self.overlay.simulation_stats.show_error)
        self.simulation.error_simulation.connect(self.streamer_overlay.simulation_stats.show_error)

        self.sbb_watcher_thread = SBBWindowCheckThread()
        self.sbb_watcher_thread.changed_foreground.connect(self.overlay.visible_in_bg)
        self.sbb_watcher_thread.changed_rect.connect(self.overlay.set_rect)

        self.resize(1300, 800)

        self.sbb_watcher_thread.start()
        self.log_updates.start()
        self.github_updates.start()
        self.simulation.start()

    def get_player_index(self, player_id: str):
        if player_id not in self.player_ids:
            self.player_ids.append(player_id)
        return self.player_ids.index(player_id)

    def new_game(self, matchmaking):
        self.in_matchmaking = matchmaking
        self.player_ids.clear()
        self.overlay.enable_hovers()
        self.overlay.turn_display.setVisible(settings.get(settings.enable_turn_display))
        self.streamer_overlay.turn_display.setVisible(settings.get(settings.enable_turn_display))
        for index in range(0, 8):
            self.comp_tabs.tabBar().setTabTextColor(index, "white")
            comp = self.comps[index]
            comp.composition = None
            comp.player = None
            comp.current_round = 0
            comp.last_seen = None

            for overlay in [self.overlay, self.streamer_overlay]:
                overlay_comp = overlay.comps[index]
                overlay_comp.composition = None
                overlay_comp.player = None
                overlay_comp.current_round = 0
                overlay_comp.last_seen = None
                overlay.simulation_stats.reset_chances()

    def end_combat(self):
        self.overlay.simulation_stats.update_labels()
        self.streamer_overlay.simulation_stats.update_labels()

    def get_comp(self, index: int):
        return self.comps[index]

    def update_round_num(self, round_number):
        self.round_indicator.setText(f"Turn {round_number} ({round_to_xp(round_number)})")
        self.round_indicator.update()
        self.overlay.update_round(round_number)

    def update_player(self, player, round_number):
        index = self.get_player_index(player.playerid)
        real_hero_name = asset_utils.get_card_name(player.heroid)
        title = f"{real_hero_name}"
        if player.health <= 0:
            self.comp_tabs.tabBar().setTabTextColor(index, "red")
            title += " *DEAD*"
        self.comp_tabs.tabBar().setTabText(index, title)
        comp = self.get_comp(index)
        comp.player = player
        comp.current_round = round_number
        self.overlay.comps[index].current_round = round_number
        self.overlay.new_places[int(player.place) - 1] = index
        self.streamer_overlay.comps[index].current_round = round_number
        self.streamer_overlay.new_places[int(player.place) - 1] = index

        self.update()

    # Somehow here update the display
    def update_comp(self, state, round_number):
        for player_id in state:
            board = state[player_id]
            index = self.get_player_index(player_id)
            comp = self.get_comp(index)

            player = comp.player
            board_player = None
            for char in board:
                if char.zone == "Hero":
                    board_player = char
                    break
            if player and board_player:
                board_player.level = player.level
            elif board_player:
                board_player.level = 0
            comp.composition = board
            comp.last_seen = round_number

            self.board_analysis.update_comp(board, round_number, player_id)
            self.overlay.update_comp(index, board, round_number)
            self.streamer_overlay.update_comp(index, board, round_number)
            self.update()

        self.overlay.simulation_stats.reset_chances()
        self.streamer_overlay.simulation_stats.reset_chances()
        self.most_recent_combat = state
        if settings.get(settings.enable_sim):
            if self.board_queue.qsize() == 0:
                self.board_queue.put((state, self.player_ids[0], settings.get(settings.number_simulations, 1000),
                                      settings.get(settings.number_threads, 3)))


    def update_stats(self, starting_hero: str, player, session_id: str, match_data):
        if settings.get(settings.upload_data) and self.in_matchmaking and session_id not in self.player_stats.df['SessionId'].values:
            # upload only matchmade games
            upload_data(match_data)
        if settings.get(settings.save_stats, True) and (
                not settings.get(settings.matchmaking_only) or self.in_matchmaking):
            place = player.place if int(player.health) <= 0 else "1"
            self.player_stats.update_stats(starting_hero, asset_utils.get_card_name(player.heroid),
                                           place, player.mmr, session_id)
            self.match_history.update_history_table()
            self.match_history.update_stats_table()
            if settings.get(settings.streamable_score_list):
                self.streamable_scores.add_score(place)
        self.overlay.disable_hovers()
        self.overlay.turn_display.setVisible(False)

    def update_health(self, player):
        index = self.get_player_index(player.playerid)
        new_place = int(player.place)
        places = self.overlay.places
        places.remove(index)
        places.insert(new_place - 1, index)

    def show_overlay(self):
        if settings.get(settings.enable_overlay):
            self.overlay.show()
        else:
            self.overlay.hide()

    def show_scores(self):
        if settings.get(settings.streamable_score_list):
            self.streamable_scores.show()
        else:
            self.streamable_scores.hide()

    def export_last_comp(self):
        if self.most_recent_combat:
            with open(paths.sbbtracker_folder.joinpath("last_combat.json"), "w") as file:
                json.dump(from_state(asset_utils.replace_template_ids(self.most_recent_combat)),
                          file, default=lambda o: o.__dict__)

    def open_url(self, url_string: str):
        url = QUrl(url_string)
        if not QDesktopServices.openUrl(url):
            QMessageBox.warning(self, 'Open Url', 'Could not open url')

    def open_discord(self):
        self.open_url('https://discord.com/invite/2AJctfj239')

    def open_github_release(self):
        self.open_url("https://github.com/SBBTracker/SBBTracker/releases/latest")

    def open_issues(self):
        self.open_url("https://github.com/SBBTracker/SBBTracker/issues")

    def show_patch_notes(self):
        try:
            with open(patch_notes_file, "r") as file:
                patch_notes = file.read()
                QMessageBox.information(self, "Patch Notes", patch_notes)
                settings.set_(settings.show_patch_notes, False)
        except Exception:
            logging.exception("Couldn't read patch notes file!")

    def handle_update(self, update_avail, patch_notes):
        if update_avail:
            self.update_banner.show()
            try:
                with open(patch_notes_file, "w") as file:
                    file.write(patch_notes)
            except Exception:
                logging.exception("Couldn't write patch notes!")

    def install_update(self):
        if paths.os_name == "Windows":
            dialog = QDialog(self)
            dialog.setWindowTitle("Updater")
            dialog_layout = QVBoxLayout(dialog)
            self.download_progress = QProgressBar(dialog)
            dialog_layout.addWidget(QLabel("Downloading update..."))
            dialog_layout.addWidget(self.download_progress)
            dialog.show()
            dialog.update()
            logging.info("Starting download...")
            updater.self_update(self.handle_progress)
            settings.set_(settings.show_patch_notes, True)
            self.close()
            sys.exit(0)
        else:
            self.open_github_release()

    def handle_progress(self, blocknum, blocksize, totalsize):

        read_data = blocknum * blocksize
        if totalsize > 0:
            download_percentage = read_data * 100 / totalsize
            logging.info(f"Download at: {download_percentage}%")

            self.download_progress.setValue(download_percentage)

            QApplication.processEvents()

    def export_csv(self):
        filepath, filetype = QFileDialog.getSaveFileName(parent=None, caption='Export to .csv',
                                                         dir=str(Path(os.environ['USERPROFILE']).joinpath("Documents")),
                                                         filter="Text CSV (*.csv)")
        if filepath:
            self.player_stats.export(Path(filepath))

    def delete_stats(self, window):
        reply = QMessageBox.question(window, "Delete all Stats", "Do you want to delete *ALL* saved stats?")
        if reply == QMessageBox.Yes:
            self.player_stats.delete()
            self.match_history.update_history_table()

    def reattach_to_log(self):
        try:
            os.remove(log_parser.offsetfile)
        except Exception as e:
            logging.warning(str(e))
        self.update()

    def closeEvent(self, *args, **kwargs):
        super(QMainWindow, self).closeEvent(*args, **kwargs)
        self.github_updates.terminate()
        self.log_updates.terminate()
        self.simulation.terminate()
        self.sbb_watcher_thread.terminate()
        self.player_stats.save()
        self.overlay.close()
        self.streamer_overlay.close()
        self.streamable_scores.close()
        settings.save()


class BoardComp(QWidget):
    def __init__(self, scale=1):
        super().__init__()
        self.composition = None
        self.golden_overlay = QPixmap(asset_utils.get_asset("golden_overlay.png"))
        self.border = QPixmap(asset_utils.get_asset("neutral_border.png"))
        self.last_seen = None
        self.current_round = 0
        self.player = None
        self.scale = scale

        self.number_display_font = QFont(display_font_family, 25, weight=QFont.ExtraBold)

    def update_card_stats(self, painter: QPainter, slot: int, health: str, attack: str):
        card_location = get_image_location(slot)
        att_center = tuple(map(operator.add, att_loc, card_location))
        health_center = tuple(map(operator.add, health_loc, card_location))
        att_circle_center = tuple(map(operator.sub, att_center, (30, 40)))
        health_circle_center = tuple(map(operator.sub, health_center, (30, 40)))

        metrics = QFontMetrics(self.number_display_font)
        att_text_center = tuple(map(operator.sub, att_center, (metrics.horizontalAdvance(attack) / 2 - 2, -4)))
        health_text_center = tuple(map(operator.sub, health_center, (metrics.horizontalAdvance(health) / 2 - 2, -4)))
        if attack:
            if slot < 7:
                painter.drawPixmap(QPoint(*att_circle_center), QPixmap(asset_utils.get_asset("attack_orb.png")))
                path = QPainterPath()
                path.addText(QPoint(*att_text_center), self.number_display_font, attack)
                painter.setPen(QPen(QColor("black"), 1))
                painter.setBrush(QBrush("white"))
                painter.drawPath(path)
        if health:
            if slot < 7 or slot == 11:
                painter.drawPixmap(QPoint(*health_circle_center), QPixmap(asset_utils.get_asset("health_orb.png")))
                path = QPainterPath()
                path.addText(QPoint(*health_text_center), self.number_display_font, health)
                painter.setPen(QPen(QColor("black"), 1))
                painter.setBrush(QBrush("white"))
                painter.drawPath(path)

    def update_card(self, painter: QPainter, slot, content_id: str, health: str,
                    attack: str, is_golden):
        card_loc = get_image_location(int(slot))
        actually_is_golden = is_golden if isinstance(is_golden, bool) else is_golden == "True"
        path = asset_utils.get_card_path(content_id, actually_is_golden)
        pixmap = QPixmap(path)
        painter.setPen(QPen(QColor("white"), 1))
        painter.drawText(card_loc[0] + 75, card_loc[1] + 100, str(content_id))
        if not settings.get(settings.show_ids):
            painter.drawPixmap(card_loc[0], card_loc[1], pixmap)
        painter.drawPixmap(card_loc[0], card_loc[1], self.border)
        if actually_is_golden:
            painter.drawPixmap(card_loc[0], card_loc[1], self.golden_overlay)
        self.update_card_stats(painter, int(slot), str(health), str(attack))

    def update_xp(self, painter: QPainter, xp: str):
        card_loc = get_image_location(11)
        xp_center = tuple(map(operator.add, xp_loc, card_loc))
        metrics = QFontMetrics(self.number_display_font)
        xp_orb_center = tuple(map(operator.sub, xp_center, (30, 40)))
        xp_text_center = tuple(map(operator.sub, xp_center, (metrics.horizontalAdvance(xp) / 2 - 2, -4)))
        painter.drawPixmap(QPoint(*xp_orb_center), QPixmap(asset_utils.get_asset("xp_orb.png")))
        path = QPainterPath()
        path.addText(QPoint(*xp_text_center), self.number_display_font, xp)
        painter.setPen(QPen(QColor("black"), 1))
        painter.setBrush(QBrush("white"))
        painter.drawPath(path)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.scale(self.scale, self.scale)
        if self.composition is not None:
            for action in self.composition:
                if action.zone != "Hero":
                    #  skip hero because we handle it elsewhere
                    #  spells broke
                    slot = action.slot
                    zone = action.zone
                    position = 10 if zone == 'Spell' else (7 + int(slot)) if zone == "Treasure" else slot
                    self.update_card(painter, position, action.content_id, action.cardhealth,
                                     action.cardattack, action.is_golden)
        else:
            painter.eraseRect(QRect(0, 0, 1350, 820))
        if self.player:
            self.update_card(painter, 11, self.player.heroid, self.player.health, "", False)
            self.update_xp(painter, f"{self.player.level}.{self.player.experience}")
        last_seen_text = ""
        if self.last_seen is not None:
            if self.last_seen == 0:
                last_seen_text = "Last seen just now"
            elif self.last_seen > 0:
                last_seen_text = f"Last seen {self.current_round - self.last_seen}"
                if self.current_round - self.last_seen == 1:
                    last_seen_text += " turn ago"
                else:
                    last_seen_text += " turns ago"
        else:
            last_seen_text = "Not yet seen"
        painter.setPen(QPen(QColor("white"), 1))
        seen_font = QFont("Roboto")
        seen_font.setPixelSize(20)
        painter.setFont(seen_font)
        painter.drawText(10, 25, last_seen_text)


class MatchHistory(QWidget):
    def __init__(self, parent, player_stats: stats.PlayerStats):
        super().__init__()
        self.parent = parent
        self.player_stats = player_stats
        self.match_history_table = QTableWidget(stats.stats_per_page, 4)
        self.page = 1
        self.display_starting_hero = 0
        self.filter_ = settings.get(settings.filter_)
        if self.filter_ not in default_dates:
            self.filter_ = "All Matches"
            settings.set_(settings.filter_, self.filter_)
        self.match_history_table.setHorizontalHeaderLabels(["Starting Hero", "Ending Hero", "Place", "+/- MMR"])
        self.match_history_table.setColumnWidth(0, 140)
        self.match_history_table.setColumnWidth(1, 140)
        self.match_history_table.setColumnWidth(2, 80)
        self.match_history_table.setColumnWidth(3, 85)
        self.match_history_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.match_history_table.verticalHeader().setSectionResizeMode(QHeaderView.Stretch)

        self.match_history_table.setContextMenuPolicy(Qt.CustomContextMenu)

        def history_menu(position):
            menu = QMenu()
            delete_action = menu.addAction("Delete")
            action = menu.exec(self.match_history_table.mapToGlobal(position))
            if action == delete_action:
                self.player_stats.delete_entry(self.match_history_table.itemAt(position).row() +
                                               (self.page - 1) * stats.stats_per_page, reverse=True)
                self.update_history_table()

        self.match_history_table.customContextMenuRequested.connect(history_menu)

        paged_table = QWidget()
        paged_table.setMaximumWidth(533)
        paged_layout = QVBoxLayout(paged_table)

        buttons_widget = QWidget()
        page_buttons = QHBoxLayout(buttons_widget)
        self.prev_button = QPushButton("<")
        self.prev_button.clicked.connect(self.page_down)
        self.prev_button.setMaximumWidth(50)
        self.next_button = QPushButton(">")
        self.next_button.setMaximumWidth(50)
        self.next_button.clicked.connect(self.page_up)

        self.page_indicator = QLabel("1")
        self.page_indicator.setFont(QFont("Roboto", 16))

        page_buttons.addWidget(self.prev_button, alignment=Qt.AlignRight)
        page_buttons.addWidget(self.page_indicator, alignment=Qt.AlignCenter | Qt.AlignVCenter)
        page_buttons.addWidget(self.next_button, alignment=Qt.AlignLeft)
        page_buttons.setSpacing(0)

        paged_layout.addWidget(self.match_history_table)
        paged_layout.addWidget(buttons_widget)
        paged_table.resize(200, paged_table.height())

        stats_widget = QWidget()
        stats_layout = QVBoxLayout(stats_widget)
        self.stats_table = QTableWidget(asset_utils.get_num_heroes() + 1, 6)
        self.stats_table.setHorizontalHeaderLabels(stats.headings)
        self.stats_table.setColumnWidth(0, 130)
        self.stats_table.setColumnWidth(1, 115)
        self.stats_table.setColumnWidth(2, 115)
        self.stats_table.setColumnWidth(3, 90)
        self.stats_table.setColumnWidth(4, 90)
        self.stats_table.setColumnWidth(5, 110)
        self.stats_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.stats_table.setFocusPolicy(Qt.NoFocus)
        self.stats_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.stats_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.stats_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.stats_table.setStyleSheet("""
QTabBar::tab:left,
QTabBar::tab:right{
  padding: 1px 0;
  width: 30px;
}""")
        self.stats_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.stats_table.horizontalHeader().sectionClicked.connect(self.sort_stats)

        filter_widget = QWidget()
        self.toggle_hero = QComboBox()
        hero_types = ["Starting Heroes", "Ending Heroes"]
        self.toggle_hero.activated.connect(self.toggle_heroes)
        self.toggle_hero.addItems(hero_types)
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(default_dates)
        index = self.filter_combo.findText(self.filter_)
        if index != -1:  # -1 for not found
            self.filter_combo.setCurrentIndex(index)
        self.filter_combo.activated.connect(self.filter_stats)
        self.sort_col = 0
        self.sort_asc = False

        filter_layout = QHBoxLayout(filter_widget)
        filter_layout.addWidget(self.toggle_hero)
        filter_layout.addWidget(self.filter_combo)

        stats_layout.addWidget(filter_widget)
        stats_layout.addWidget(self.stats_table)

        tables_layout = QHBoxLayout(self)
        tables_layout.addWidget(paged_table)
        tables_layout.addWidget(stats_widget)

        table_font = QFont("Roboto")
        table_font.setPixelSize(14)

        self.update_history_table()
        self.update_stats_table()

    def page_up(self):
        if self.page < self.player_stats.get_num_pages():
            self.page += 1
        self.update_history_table()

    def page_down(self):
        if self.page > 1:
            self.page -= 1
        self.update_history_table()

    def update_history_table(self):
        history = self.player_stats.get_page(self.page)
        update_table(self.match_history_table, history)
        start_num = (self.page - 1) * stats.stats_per_page + 1
        self.match_history_table.setVerticalHeaderLabels([str(i) for i in range(start_num,
                                                                                start_num + stats.stats_per_page + 1)])
        self.page_indicator.setText(f'Page {self.page} of {max(1, self.player_stats.get_num_pages())}')

    def update_stats_table(self):
        start, end = get_date_range(self.filter_)
        hero_stats = self.player_stats.filter(start, end, self.sort_col, self.sort_asc)
        chosen_stats = hero_stats[self.display_starting_hero]
        update_table(self.stats_table, chosen_stats)

    def toggle_heroes(self, index: int):
        self.display_starting_hero = index
        self.update_stats_table()

    def filter_stats(self):
        self.filter_ = self.filter_combo.currentText()
        settings.set_(settings.filter_, self.filter_)
        self.update_stats_table()

    def sort_stats(self, index: int):
        # ▼ ▲
        self.sort_asc = (self.sort_col == index) and (not self.sort_asc)
        self.sort_col = index
        headings = stats.headings.copy()
        headings[index] = headings[index] + ("▼" if self.sort_asc else "▲")
        self.stats_table.setHorizontalHeaderLabels(headings)
        self.update_stats_table()


class LiveGraphs(QWidget):
    def __init__(self):
        super().__init__()
        self.layout = QVBoxLayout(self)
        self.user_palette = settings.get(settings.live_palette)
        self.states = None

        self.health_canvas = FigureCanvasQTAgg(plt.Figure(figsize=(13.5, 18)))
        self.xp_canvas = FigureCanvasQTAgg(plt.Figure(figsize=(13.5, 18)))
        self.health_ax = self.health_canvas.figure.subplots()
        self.xp_ax = self.xp_canvas.figure.subplots()

        graphs_tabs = QTabWidget(self)
        graphs_tabs.addTab(self.health_canvas, "Health Graph")
        graphs_tabs.addTab(self.xp_canvas, "XP Graph")
        self.layout.addWidget(graphs_tabs)

    def set_color_palette(self, palette):
        self.user_palette = palette
        self.update_graph()

    def update_graph(self, states: graphs.LivePlayerStates = None):
        if states:
            self.states = states

        if self.states:
            self.xp_ax.cla()
            graphs.xp_graph(self.states, self.xp_ax, self.user_palette)
            self.xp_canvas.draw()

            self.health_ax.cla()
            graphs.live_health_graph(self.states, self.health_ax, self.user_palette)
            self.health_canvas.draw()


class BoardAnalysis(QWidget):
    def __init__(self, size, player_ids):
        super().__init__()
        self.player_ids = player_ids
        self.layout = QVBoxLayout(self)

        self.last_brawl = QSplitter(Qt.Vertical)
        # Submit analysis button
        btn_widget = QWidget()
        btn_layout = QHBoxLayout(btn_widget)
        submit_button = QPushButton("Submit")
        submit_button.clicked.connect(self.run_simulations)
        btn_layout.addWidget(submit_button)
        self.last_brawl.addWidget(btn_widget)
        # Submit analysis tab
        self.opponent_board = BoardComp(scale=0.5)
        self.player_board = BoardComp(scale=0.5)
        # opponent first to kinda look like a brawl
        self.last_brawl.addWidget(self.opponent_board)
        self.last_brawl.addWidget(self.player_board)

        # Simulation results tab
        self.sim_results = QWidget()
        self.simulated_stats = BoardAnalysisSimulationResults(self.sim_results)
        self.sim_results.move(600, 0)
        self.results_board = BoardComp(scale=0.5)
        self.results_board.move(600, 0)

        # Put it all together
        self.layout.addWidget(self.sim_results)
        self.layout.addWidget(self.results_board)
        self.layout.addWidget(self.last_brawl)

        self.analysis_queue = Queue()
        self.simulation_manager = SimulationManager(
            self.analysis_queue, self.simulated_stats, self.results_board,
        )
        self.simulation_manager.start()


    def set_color_palette(self, palette):
        self.user_palette = palette
        self.update_graph()

    def update_comp(self, player, round_number, player_id):
        # The person playing is player_ids[0]
        if player_id == self.player_ids[0]:
            comp = self.player_board
        else:
            comp = self.opponent_board
        comp.composition = player
        comp.last_seen = round_number
        self.update()

    def run_simulations(self):
        board = {
            "player": self.player_board.composition,
            "opponent": self.opponent_board.composition,
        }
        self.analysis_queue.put(
            board
        )
        # clear board results:
        self.simulated_stats.reset_chances()

class ActiveCondition:
    def __init__(self, board):
        # win, tie, loss, win_dmg, loss_dmg
        self.board = board

    def update_reward(self, win, tie, loss, win_dmg, loss_dmg):
        self.win = float(win[:-1]) / 100
        self.tie = float(tie[:-1]) / 100
        self.loss = float(loss[:-1]) / 100
        self.win_dmg = float(win_dmg)
        self.loss_dmg = float(loss_dmg)

    def chances(self):
        return (
            f"{self.win*100:.2f}%",
            f"{self.tie*100:.2f}%",
            f"{self.loss*100:.2f}%",
            f"{self.win_dmg:.2f}",
            f"{self.loss_dmg:.2f}",
        )


class BoardAnalysisSimulationResults(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.sim_is_done = True
        self.parent = parent

        self.layout = QStackedLayout(self)
        background = QFrame(self)
        self.layout.addWidget(background)

        self.win_dmg_label = QLabel("-", background)
        self.win_label = QLabel("-", background)
        self.tie_label = QLabel("-", background)
        self.loss_label = QLabel("-", background)
        self.loss_dmg_label = QLabel("-", background)

        self.win_dmg = "-"
        self.win = "-"
        self.loss = "-"
        self.tie = "-"
        self.loss_dmg = "-"
        self.displayable = False

        label_layout = QGridLayout(background)

        win_dmg_title = QLabel("DMG")
        win_percent_title = QLabel("WIN")
        win_dmg_title.setStyleSheet("QLabel { color : #9FD4A3 }")
        win_percent_title.setStyleSheet("QLabel { color : #9FD4A3 }")

        loss_dmg_title = QLabel("DMG")
        loss_percent_title = QLabel("LOSS")
        loss_dmg_title.setStyleSheet("QLabel { color : #e3365c }")
        loss_percent_title.setStyleSheet("QLabel { color : #e3365c }")

        tie_title = QLabel("TIE")

        self.error_widget = QFrame(self)
        error_layout = QVBoxLayout(self.error_widget)
        self.error_msg = QLabel("Error in simulation!")
        error_layout.addWidget(self.error_msg, alignment=Qt.AlignCenter)
        error_layout.setContentsMargins(0, 0, 0, 0)
        self.error_msg.setStyleSheet(
            "QLabel#sim-error { text-align: center; font-size: 20px; background-color: rgba(0,0,0,0%); }")
        self.error_msg.setObjectName("sim-error")
        self.error_msg.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        label_layout.addWidget(SimStatWidget(self, win_dmg_title, self.win_dmg_label), 0, 0)
        label_layout.addWidget(SimStatWidget(self, win_percent_title, self.win_label), 0, 1)
        label_layout.addWidget(SimStatWidget(self, tie_title, self.tie_label), 0, 2)
        label_layout.addWidget(SimStatWidget(self, loss_percent_title, self.loss_label), 0, 3)
        label_layout.addWidget(SimStatWidget(self, loss_dmg_title, self.loss_dmg_label), 0, 4)

        self.layout.addWidget(self.error_widget)
        self.error_widget.setMinimumSize(background.minimumSize())

        label_layout.setSpacing(0)
        label_layout.setRowStretch(0, 1)
        label_layout.setRowStretch(1, 1)
        label_layout.setContentsMargins(0, 0, 0, 0)

    def reset_chances(self):
        self.win_dmg = "-"
        self.win = "-"
        self.tie = "-"
        self.loss = "-"
        self.loss_dmg = "-"
        self.win_dmg_label.setText(self.win_dmg)
        self.win_label.setText(self.win)
        self.loss_label.setText(self.loss)
        self.tie_label.setText(self.tie)
        self.loss_dmg_label.setText(self.loss_dmg)
        self.displayable = False
        self.layout.setCurrentIndex(0)

    def update_chances(self, win, tie, loss, win_dmg, loss_dmg):
        self.win_dmg = win_dmg
        self.win = win
        self.loss = loss
        self.tie = tie
        self.loss_dmg = loss_dmg
        self.update_labels()
        self.displayable = False

    def show_error(self, msg: str):
        self.error_msg.setText(msg)
        self.layout.setCurrentIndex(1)

    def update_labels(self):
        self.win_dmg_label.setText(self.win_dmg)
        self.win_label.setText(self.win)
        self.loss_label.setText(self.loss)
        self.tie_label.setText(self.tie)
        self.loss_dmg_label.setText(self.loss_dmg)
        self.displayable = True

    def sim_end(self, *args):
        self.sim_is_done = True


class StatsGraph(QWidget):
    def __init__(self, player_stats: stats.PlayerStats):
        super().__init__()
        self.player_stats = player_stats

        self.figure = None
        self.canvas = FigureCanvasQTAgg(plt.Figure(figsize=(13.5, 18)))
        self.ax = self.canvas.figure.subplots()

        self.graph_selection = QComboBox()
        self.graph_selection.setMaximumWidth(200)
        self.graph_selection.addItems([graphs.matches_per_hero, graphs.mmr_change])
        self.graph_selection.activated.connect(self.update_graph)
        self.selection = graphs.mmr_change

        self.layout = QVBoxLayout(self)
        self.layout.addWidget(self.graph_selection)
        self.layout.addWidget(self.canvas)

        self.update_graph()

    def update_graph(self):
        self.selection = self.graph_selection.currentText()
        self.ax.cla()
        self.figure = graphs.stats_graph(self.player_stats.df, self.selection, self.ax)
        self.canvas.draw()


def portrait_location(resolution: (int, int)):
    x, y = resolution
    return 0.346 * y - 126


def get_hover_size(resolution: (int, int)):
    x, y = resolution
    width = 0.0773 * y + 0.687
    return width, width * 17 / 21


hover_size = (84, 68)
p1_loc = (38, 247)
hover_distance = 15
base_size = (1920, 1080)


def move_point_by_scale(x, y, scale):
    monitor_at_point = QGuiApplication.screenAt(QPoint(x, y))
    primary = QGuiApplication.primaryScreen()
    new_x, new_y = x * scale, y * scale
    if primary != monitor_at_point and primary and monitor_at_point:
        dimensions = monitor_at_point.size()
        if not (0 < x < dimensions.width()):
            if x < 0:
                new_x = x + dimensions.width()
            elif x > dimensions.width():
                new_x = x - dimensions.width()
            scaled_x = new_x * scale
            new_x = x - scaled_x
        if not (0 < y < dimensions.height()):
            if y < 0:
                new_y = y + dimensions.height()
            elif y > dimensions.height():
                new_y = y - dimensions.height()
            scaled_y = new_y * scale
            new_y = y - scaled_y

    return new_x, new_y


class OverlayWindow(QMainWindow):
    simluation_update = Signal(str, str, str, str, str)

    def __init__(self, main_window):
        super().__init__()
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.SubWindow)
        self.setWindowTitle("SBBTrackerOverlay")

        main_widget = QWidget()
        main_widget.setObjectName("overlay")
        main_widget.setStyleSheet("QWidget#overlay {background-color: rgba(0, 0, 0, 0);}")

        self.show_comps = settings.get(settings.enable_comps)
        self.main_window = main_window
        self.stream_overlay = None
        self.visible = True
        self.scale_factor = 1
        self.sbb_rect = QRect(0, 0, 1920, 1080)
        self.dpi_scale = 1
        self.hover_regions = [
            HoverRegion(main_widget, *map(operator.mul, hover_size, (self.scale_factor, self.scale_factor))) for _ in
            range(0, 8)]
        self.simulation_stats = SimulatorStats(main_widget)
        self.simulation_stats.setVisible(settings.get(settings.enable_sim))
        self.turn_display = TurnDisplay(main_widget)
        self.turn_display.setVisible(False)

        self.show_hide = True

        self.comps = [BoardComp() for _ in range(0, 8)]
        self.comp_widgets = [QFrame(main_widget) for _ in range(0, 8)]
        self.places = list(range(0, 8))
        self.new_places = list(range(0, 8))
        self.base_comp_size = QSize(1100, 650)
        for index in range(len(self.comps)):
            comp = self.comps[index]
            widget = self.comp_widgets[index]

            comp.setParent(widget)
            widget.setVisible(False)
            widget.move(round(self.size().width() / 2 - 100), 0)
        self.set_transparency()
        self.update_comp_scaling()

        self.show_button = QPushButton("Show Tracker", main_widget)
        self.show_button.clicked.connect(self.show_hide_main_window)
        self.show_button.move(40, 40)
        self.show_button.resize(self.show_button.sizeHint().width(), self.show_button.sizeHint().height())
        self.show_button.setVisible(settings.get(settings.show_tracker_button))

        self.setCentralWidget(main_widget)

        self.disable_hovers()

    def show_hide_main_window(self):
        if self.show_hide:
            self.main_window.setWindowState(Qt.WindowState.WindowActive)
            self.show_button.setText("Hide Tracker")
        else:
            self.main_window.showMinimized()
            self.show_button.setText("Show Tracker")
        self.show_hide = not self.show_hide

    def visible_in_bg(self, visible):
        if settings.get(settings.hide_overlay_in_bg) and settings.get(settings.enable_overlay):
            self.visible = visible
            self.setVisible(visible)

    def set_comps_enabled(self, state: bool):
        self.show_comps = state
        if state:
            self.enable_hovers()
        else:
            self.disable_hovers()

    def disable_hovers(self):
        for hover in self.hover_regions:
            hover.setVisible(False)

    def enable_hovers(self):
        if self.show_comps:
            for hover in self.hover_regions:
                hover.setVisible(True)

    def show_hide_comp(self, index, show_or_hide: bool):
        widget = self.comp_widgets[self.places[index]]
        widget.setVisible(show_or_hide)
        if self.stream_overlay:
            streamer_widget = self.stream_overlay.comp_widgets[self.places[index]]
            streamer_widget.setVisible(show_or_hide)

    def update_round(self, round_num):
        self.turn_display.update_label(f"Turn {round_num} ({round_to_xp(round_num)})")
        if self.stream_overlay:
            self.stream_overlay.update_round(round_num)

    def update_comp(self, index, player, round_number):
        comp = self.comps[index]
        comp.composition = player
        comp.last_seen = round_number
        self.update()

    def update_comp_scaling(self):
        for i in range(len(self.comps)):
            comp = self.comps[i]
            widget = self.comp_widgets[i]
            comp.scale = settings.get(settings.overlay_comps_scaling) / 100
            comp.setFixedSize(self.base_comp_size * comp.scale)
            widget.setFixedSize(self.base_comp_size * comp.scale)
            widget.updateGeometry()

    def update_placements(self):
        self.places = self.new_places.copy()
        self.new_places = list(range(0, 8))
        for widget in self.comp_widgets:
            #  fixes bug where hovering over the hero at the end of combat gets the overlay stuck
            widget.setVisible(False)

    def set_rect(self, left, top, right, bottom, dpi):
        self.dpi_scale = 1 / round(dpi / 96 - .24)  # round .75 and up to nearest int
        if settings.get(settings.disable_scaling):
            # Windows 8 scaling is different and I don't want to deal with it
            self.dpi_scale = 1
        left_edge = left
        top_edge = top
        right_edge = right - left
        bottom_edge = bottom - top
        if self.dpi_scale != 1:
            left_edge, top_edge = move_point_by_scale(left, top, self.dpi_scale)
            right_edge *= self.dpi_scale
            bottom_edge *= self.dpi_scale

        self.sbb_rect = QRect(left_edge, top_edge, right_edge, bottom_edge)
        sbb_is_visible = QGuiApplication.screenAt(self.sbb_rect.topLeft()) is not None or QGuiApplication.screenAt(
            self.sbb_rect.bottomRight()) is not None
        if sbb_is_visible:
            self.setFixedSize(self.sbb_rect.size())
            self.setGeometry(QGuiApplication.screens()[0].geometry())
            self.move(left_edge, top_edge)
            self.scale_factor = self.sbb_rect.size().height() / base_size[1]
            self.update_hovers()
            for widget in self.comp_widgets:
                widget.move(QPoint(round(self.size().width() / 2 - 100), 0) * self.dpi_scale)

            sim_pos = QPoint(*settings.get(settings.simulator_position, (self.sbb_rect.top() / 2 - 100, 0)))
            if not self.centralWidget().geometry().contains(sim_pos):
                sim_pos = QPoint(0, 0)
            self.simulation_stats.move(sim_pos * self.dpi_scale)
            turn_pos = QPoint(*settings.get(settings.turn_indicator_position, (self.sbb_rect.top() - 300, 0)))
            if not self.centralWidget().geometry().contains(turn_pos):
                turn_pos = QPoint(0, 0)
            self.turn_display.move(turn_pos * self.dpi_scale)
            self.turn_display.label.setFont(QFont("Roboto", int(settings.get(settings.turn_display_font_size))))
            self.turn_display.update()
            if settings.get(settings.streaming_mode) and self.stream_overlay is not None:
                self.stream_overlay.set_rect(left, top, right, bottom, dpi)

    def update_hovers(self):
        true_scale = self.scale_factor
        for i in range(len(self.hover_regions)):
            hover = self.hover_regions[i]
            loc = QPoint(38 * true_scale,
                         portrait_location((self.sbb_rect.size() / self.dpi_scale).toTuple()) * self.dpi_scale +
                         hover_distance * i * true_scale +
                         hover_size[1] * true_scale * i)
            hover.move(loc)
            new_size = QSize(*get_hover_size(self.sbb_rect.size().toTuple()))
            hover.resize(new_size)
            hover.background.setFixedSize(new_size)
            hover.enter_hover.connect(lambda y=i: self.show_hide_comp(y, True))
            hover.leave_hover.connect(lambda y=i: self.show_hide_comp(y, False))
        self.update()

    def set_transparency(self):
        alpha = (100 - settings.get(settings.boardcomp_transparency, 0)) / 100
        style = f"background-color: rgba({default_bg_color_rgb}, {alpha});"
        for widget in self.comp_widgets:
            widget.setStyleSheet(style)

        alpha = (100 - settings.get(settings.simulator_transparency, 0)) / 100
        style = f"background-color: rgba({default_bg_color_rgb}, {alpha}); font-size: 17px"
        self.simulation_stats.setStyleSheet(style)

    def toggle_transparency(self):
        if settings.get(settings.streaming_mode):
            self.setWindowFlags(self.windowFlags() | Qt.SubWindow)
        else:
            self.setWindowFlags(Qt.SubWindow)


class StreamerOverlayWindow(OverlayWindow):
    def __init__(self, main_window):
        super().__init__(main_window)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setWindowFlags(self.windowFlags() &
                            ~Qt.WindowStaysOnTopHint & ~Qt.SubWindow
                            | Qt.WindowStaysOnBottomHint)
        self.centralWidget().setStyleSheet(
            f"QWidget#overlay {{background-color: {settings.get(settings.stream_overlay_color)} ;}}")
        self.show_button.hide()
        self.setFixedSize(*settings.get(settings.streamer_overlay_size))
        self.disable_hovers()

    def set_transparency(self):
        alpha = 1
        style = f"background-color: rgba({default_bg_color_rgb}, {alpha});"
        for widget in self.comp_widgets:
            widget.setStyleSheet(style)

        alpha = 1
        style = f"background-color: rgba({default_bg_color_rgb}, {alpha}); font-size: 17px"
        self.simulation_stats.setStyleSheet(style)

    def set_rect(self, left, top, right, bottom, dpi):
        super().set_rect(left, top, right, bottom, dpi)
        settings.set_(settings.streamer_overlay_size, self.sbb_rect.size().toTuple())


class SimStatWidget(QFrame):
    def __init__(self, parent, title: QLabel, value: QLabel):
        super().__init__(parent)
        self.title = title
        self.value = value

        layout = QVBoxLayout(self)
        layout.addWidget(title, alignment=Qt.AlignHCenter)
        layout.addWidget(value, alignment=Qt.AlignHCenter)
        layout.setSpacing(20)
        title.setAttribute(Qt.WA_TranslucentBackground)
        title.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Maximum)
        value.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(80)
        self.setStyleSheet("background-color: rgba(0,0,0,0%);")


class MovableWidget(QWidget):
    def __init__(self, parent, setting: settings.Setting):
        super().__init__(parent)
        self.setting = setting

    def set_setting(self, setting):
        self.setting = setting

    def mousePressEvent(self, event):
        self._mousePressed = True
        self._mousePos = event.globalPosition().toPoint()
        self._windowPos = self.pos()

    def mouseMoveEvent(self, event):
        if self._mousePressed and (Qt.LeftButton & event.buttons()):
            self.move(self._windowPos +
                      (event.globalPosition().toPoint() - self._mousePos))

    def mouseReleaseEvent(self, event):
        if self.setting:
            settings.set_(self.setting, self.pos().toTuple())


class SimulatorStats(MovableWidget):
    def __init__(self, parent):
        super().__init__(parent, settings.simulator_position)
        self.parent = parent

        self.layout = QStackedLayout(self)
        background = QFrame(self)
        # background.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Maximum)
        # self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Maximum)
        self.layout.addWidget(background)

        self.win_dmg_label = QLabel("-", background)
        self.win_label = QLabel("-", background)
        self.tie_label = QLabel("-", background)
        self.loss_label = QLabel("-", background)
        self.loss_dmg_label = QLabel("-", background)

        self.win_dmg = "-"
        self.win = "-"
        self.loss = "-"
        self.tie = "-"
        self.loss_dmg = "-"
        self.displayable = False

        label_layout = QGridLayout(background)

        win_dmg_title = QLabel("DMG")
        win_percent_title = QLabel("WIN")
        win_dmg_title.setStyleSheet("QLabel { color : #9FD4A3 }")
        win_percent_title.setStyleSheet("QLabel { color : #9FD4A3 }")

        loss_dmg_title = QLabel("DMG")
        loss_percent_title = QLabel("LOSS")
        loss_dmg_title.setStyleSheet("QLabel { color : #e3365c }")
        loss_percent_title.setStyleSheet("QLabel { color : #e3365c }")

        tie_title = QLabel("TIE")

        self.error_widget = QFrame(self)
        error_layout = QVBoxLayout(self.error_widget)
        self.error_msg = QLabel("Error in simulation!")
        error_layout.addWidget(self.error_msg, alignment=Qt.AlignCenter)
        error_layout.setContentsMargins(0, 0, 0, 0)
        self.error_msg.setStyleSheet(
            "QLabel#sim-error { text-align: center; font-size: 20px; background-color: rgba(0,0,0,0%); }")
        self.error_msg.setObjectName("sim-error")
        self.error_msg.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        label_layout.addWidget(SimStatWidget(self, win_dmg_title, self.win_dmg_label), 0, 0)
        label_layout.addWidget(SimStatWidget(self, win_percent_title, self.win_label), 0, 1)
        label_layout.addWidget(SimStatWidget(self, tie_title, self.tie_label), 0, 2)
        label_layout.addWidget(SimStatWidget(self, loss_percent_title, self.loss_label), 0, 3)
        label_layout.addWidget(SimStatWidget(self, loss_dmg_title, self.loss_dmg_label), 0, 4)

        self.layout.addWidget(self.error_widget)
        self.error_widget.setMinimumSize(background.minimumSize())

        label_layout.setSpacing(0)
        label_layout.setRowStretch(0, 1)
        label_layout.setRowStretch(1, 1)
        label_layout.setContentsMargins(0, 0, 0, 0)

    def reset_chances(self):
        self.win_dmg = "-"
        self.win = "-"
        self.tie = "-"
        self.loss = "-"
        self.loss_dmg = "-"
        self.win_dmg_label.setText(self.win_dmg)
        self.win_label.setText(self.win)
        self.loss_label.setText(self.loss)
        self.tie_label.setText(self.tie)
        self.loss_dmg_label.setText(self.loss_dmg)
        self.displayable = False
        self.layout.setCurrentIndex(0)

    def update_chances(self, win, tie, loss, win_dmg, loss_dmg):
        self.win_dmg = win_dmg
        self.win = win
        self.loss = loss
        self.tie = tie
        self.loss_dmg = loss_dmg
        if self.displayable:
            self.update_labels()
        self.displayable = False

    def show_error(self, msg: str):
        self.error_msg.setText(msg)
        self.layout.setCurrentIndex(1)

    def update_labels(self):
        self.win_dmg_label.setText(self.win_dmg)
        self.win_label.setText(self.win)
        self.loss_label.setText(self.loss)
        self.tie_label.setText(self.tie)
        self.loss_dmg_label.setText(self.loss_dmg)
        self.displayable = True


class TurnDisplay(MovableWidget):
    def __init__(self, parent):
        super().__init__(parent, settings.turn_indicator_position)
        layout = QHBoxLayout(self)
        frame = QFrame(self)
        frame_layout = QHBoxLayout(frame)
        self.label = QLabel("Turn 0 (0.0)", frame)
        frame.setStyleSheet(f"QFrame {{ background-color: {default_bg_color}}};")
        self.label.setFont(QFont("Roboto", int(settings.get(settings.turn_display_font_size))))
        layout.addWidget(frame)
        frame_layout.addWidget(self.label, Qt.AlignVCenter)

        layout.setContentsMargins(0, 0, 0, 0)

        layout.setSizeConstraint(QLayout.SetMinimumSize)
        frame_layout.setSizeConstraint(QLayout.SetMinimumSize)

    def update_label(self, text):
        self.label.setText(text)


class HoverRegion(QWidget):
    enter_hover = Signal()
    leave_hover = Signal()

    def __init__(self, parent, width, height):
        super().__init__(parent)
        self.background = QWidget(self)
        self.background.setMinimumSize(width, height)
        self.setStyleSheet("background-color: rgba(0, 0, 0, 0.01);")
        if DEBUG:
            self.setStyleSheet("background-color: rgba(255, 255, 255, 0.5);")
        self.setMinimumSize(width, height)

    def enterEvent(self, event):
        self.enter_hover.emit()

    def leaveEvent(self, event):
        self.leave_hover.emit()


class StreamableMatchDisplay(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnBottomHint)
        self.setWindowTitle("SBBTracker Scores")
        self.scores = settings.get(settings.streamable_scores)
        self.label = QLabel("Scores:")
        self.label.setStyleSheet("QLabel { font-size: 50px; background-color: #00FFFF;}")
        self.setCentralWidget(self.label)
        self.update_label()

    def add_score(self, score):
        self.scores.append(score)
        settings.set_(settings.streamable_scores, self.scores)
        self.update_label()

    def update_label(self):
        display_text = "Scores: "
        max_scores = settings.get(settings.streamable_score_max_len)
        for i in range(0, len(self.scores)):
            score = self.scores[i]
            display_text += f"{score} "
            if (i + 1) % max_scores == 0 and i != 0:
                display_text += '\n               '

        self.label.setText(display_text)

    def reset(self):
        self.scores = []
        settings.set_(settings.streamable_scores, [])
        self.update_label()

    def mousePressEvent(self, event):
        self._mousePressed = True
        self._mousePos = event.globalPosition().toPoint()
        self._windowPos = self.pos()

    def mouseMoveEvent(self, event):
        if self._mousePressed and (Qt.LeftButton & event.buttons()):
            self.move(self._windowPos +
                      (event.globalPosition().toPoint() - self._mousePos))


def main():
    multiprocessing.freeze_support()
    app = QApplication(sys.argv)
    pixmap = QPixmap(asset_utils.get_asset("icon.png"))
    splash = QSplashScreen(pixmap)
    splash.show()

    app.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.RoundPreferFloor)
    apply_stylesheet(app, theme='dark_teal.xml')
    stylesheet = app.styleSheet()
    stylesheet = stylesheet.replace("""QTabBar::tab {
      color: #ffffff;
      border: 0px;
    }""", """QTabBar::tab {
      border: 0px;
    }""") + "QTabBar{ text-transform: none; }"
    app.setStyleSheet(stylesheet)

    stats.backup_stats()

    # TODO: uncomment this when the updater doesn't require input

    #     if settings.silent_updates not in settings:
    #         reply = QMessageBox.question(None, "Enable silent updates?",
    #                                      f"""Would you like to enable silent updates?
    # This will allow the application to update automatically
    # when you open it (if there's an update).
    #
    # You can change this setting at any time at:
    # settings > Updates > Enable silent updates
    # """)
    #         settings.set(settings.silent_updates, reply == QMessageBox.Yes)
    #         settings.save()

    if settings.get(settings.prompt_data_collection) and not settings.get(settings.upload_data):
        settings.set_(settings.prompt_data_collection, False)
        reply = QMessageBox.question(None, "Opt-in to data collection?",
                                     """Would you like to opt into to data collection?
This enables you to upload your matches to sbbtracker.com
and contribute to the community dataset.

Things that are currently collected:
* Your steam name (this is not shared)
* Board states
* Placement
* Hero, Health, and XP
* Net MMR

This may include more game-related information as we improve
our ability to collect it.

You may change your selection at any time at Settings > Data > Upload Matches""")
        settings.set_(settings.upload_data, reply == QMessageBox.Yes)
        settings.save()

    main_window = SBBTracker()
    main_window.show()
    splash.finish(main_window)
    if settings.get(settings.show_patch_notes, False):
        main_window.show_patch_notes()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
