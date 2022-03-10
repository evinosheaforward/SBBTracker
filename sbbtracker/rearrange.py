import copy

import numpy as np

from sbbtracker.parsers.log_parser import Action
from sbbtracker.utils.asset_utils import replace_template_ids

from sbbbattlesim.board import Board
from sbbbattlesim.characters import registry as character_registry
from sbbbattlesim.heroes import registry as hero_registry
from sbbbattlesim.player import PlayerOnSetup
from sbbbattlesim.simulate import from_state
from sbbbattlesim.treasures import registry as treasure_registry


def make_swap(
    board,
    slot_orig,
    slot_dest,
):
    return apply_permutation(
        board,
        permute_map = {
            # in the UI/ logs the slot is 0-6
            # in the sim the "position" is 1-7
            slot_orig+1: slot_dest+1,
            slot_dest+1: slot_orig+1,
        }
    )


def randomize_board(board):
    return apply_permutation(
        board,
        permute_map={
            orig: perm
            for orig, perm in zip(
                list(range(1, 8)), np.random.permutation(list(range(1, 8)))
            )
        }
    )


def apply_permutation(board, permute_map):
    # don't permute in place
    board_stated = Board(
        from_state(
            copy.deepcopy(
                replace_template_ids(
                    board
                )
            )
        )
    )
    board_stated("OnSetup")
    characters = board_stated.p1.valid_characters()
    board_stated.p1.despawn(*characters, kill=False)

    for character in characters:
        board_stated.p1.spawn(
            character,
            position=permute_map.get(character.position, character.position),
        )


    return convert_state_to_action_json(board_stated)


def convert_state_to_action_json(board):
    converted_board = dict()

    for player, details in board.items():
        converted_details = dict()
        converted_details['treasures'] = [
            {
                "content_id": treasure,
                "playerid": player
            }
            for treasure in details['treasures']
        ]

        converted_details['characters'] = [
            {
                'slot': str(character['position'] - 1),
                'content_id': character,
                'cardattack': character['attack'],
                'cardhealth': character['health'],
                'is_golden': character['golden'],
                'cost': character['cost'],
                'subtypes': character['tribes'],  # NOTE, does this get us the capitalization we desire?,
                'playerid': player,
            }
            for character in details['characters']
        ]

        converted_details['hero'] = {
            'hero': details['hero'],
            'playerid': player,
            'content_id': details['hero'],
        }

        converted_details['spells'] = [
             {
                 "playerid": player,
                 "content_id": spell
             }
             for spell in details['spells']
        ]

        converted_board[player] = converted_details

    return converted_board
