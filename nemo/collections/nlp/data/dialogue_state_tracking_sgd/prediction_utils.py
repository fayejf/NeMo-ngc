# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Prediction and evaluation-related utility functions.
This file contains code artifacts adapted from the original implementation:
https://github.com/google-research/google-research/blob/master/schema_guided_dst/baseline/pred_utils.py
"""

import json
import os
from collections import OrderedDict, defaultdict

from nemo.collections.nlp.data.dialogue_state_tracking_sgd.input_example import (
    STATUS_ACTIVE,
    STATUS_DONTCARE,
    STATUS_OFF,
    STR_DONTCARE,
)
from nemo.utils import logging

REQ_SLOT_THRESHOLD = 0.5


# MIN_SLOT_RELATION specifes the minimum number of relations between two slots in the training dialogues to get considered for carry-over
MIN_SLOT_RELATION = 25

__all__ = ['write_predictions_to_file']


def set_cat_slot(predictions_status, predictions_value, cat_slots, cat_slot_values, sys_slots_agg, cat_value_thresh):
    """
    write predicted slot and values into out_dict 
    """
    out_dict = {}
    for slot_idx, slot in enumerate(cat_slots):
        slot_status = predictions_status[slot_idx][0]["cat_slot_status"]
        if slot_status == STATUS_DONTCARE:
            out_dict[slot] = STR_DONTCARE
        elif slot_status == STATUS_ACTIVE:
            tmp = predictions_value[slot_idx]
            value_idx = max(tmp, key=lambda k: tmp[k]['cat_slot_value_status'][0].item())
            value_prob = max([v['cat_slot_value_status'][0].item() for k, v in predictions_value[slot_idx].items()])
            if sys_slots_agg is None or value_prob > cat_value_thresh:
                out_dict[slot] = cat_slot_values[slot][value_idx]
            elif slot in sys_slots_agg:
                # retrieval
                out_dict[slot] = sys_slots_agg[slot]
    return out_dict


def set_noncat_slot(
    predictions_status, predictions_value, non_cat_slots, user_utterance, sys_slots_agg, non_cat_value_thresh
):
    """
    write predicted slot and values into out_dict 
    """
    out_dict = {}
    for slot_idx, slot in enumerate(non_cat_slots):
        slot_status = predictions_status[slot_idx][0]["noncat_slot_status"]
        if slot_status == STATUS_DONTCARE:
            out_dict[slot] = STR_DONTCARE
        elif slot_status == STATUS_ACTIVE:
            value_prob = predictions_value[slot_idx][0]["noncat_slot_p"]
            tok_start_idx = predictions_value[slot_idx][0]["noncat_slot_start"]
            tok_end_idx = predictions_value[slot_idx][0]["noncat_slot_end"]
            ch_start_idx = predictions_value[slot_idx][0]["noncat_alignment_start"][tok_start_idx]
            ch_end_idx = predictions_value[slot_idx][0]["noncat_alignment_end"][tok_end_idx]
            if ch_start_idx > 0 and ch_end_idx > 0:
                # Add span from the user utterance.
                out_dict[slot] = user_utterance[ch_start_idx - 1 : ch_end_idx]
            elif sys_slots_agg and slot in sys_slots_agg:
                # system retrieval
                out_dict[slot] = sys_slots_agg[slot]
    return out_dict


def get_predicted_dialog(dialog, all_predictions, schemas, state_tracker, cat_value_thresh, non_cat_value_thresh):
    # Overwrite the labels in the turn with the predictions from the model. For
    # test set, these labels are missing from the data and hence they are added.
    dialog_id = dialog["dialogue_id"]
    if state_tracker == "baseline":
        sys_slots_agg = {}
    else:
        sys_slots_agg = defaultdict(OrderedDict)
    all_slot_values = defaultdict(dict)
    for turn_idx, turn in enumerate(dialog["turns"]):
        if turn["speaker"] == "SYSTEM" and state_tracker == 'nemotracker':
            # sys_slots_last = defaultdict(OrderedDict)
            for frame in turn["frames"]:
                if frame["service"] not in sys_slots_agg:
                    sys_slots_agg[frame["service"]] = OrderedDict()
                for action in frame["actions"]:
                    if action["slot"] and len(action["values"]) > 0:
                        sys_slots_agg[frame["service"]][action["slot"]] = action["values"][0]
        if turn["speaker"] == "USER":
            user_utterance = turn["utterance"]
            system_utterance = dialog["turns"][turn_idx - 1]["utterance"] if turn_idx else ""
            system_user_utterance = system_utterance + ' ' + user_utterance
            turn_id = "{:02d}".format(turn_idx)
            for frame in turn["frames"]:

                predictions = all_predictions[(dialog_id, turn_id, frame["service"])]
                slot_values = all_slot_values[frame["service"]]
                service_schema = schemas.get_service_schema(frame["service"])
                # Remove the slot spans and state if present.
                frame.pop("slots", None)
                frame.pop("state", None)

                # The baseline model doesn't predict slot spans. Only state predictions
                # are added.
                state = {}

                # Add prediction for active intent. No Offset is subtracted since schema has now NONE intent at index 0
                state["active_intent"] = get_predicted_intent(
                    predictions=predictions[0], intents=service_schema.intents
                )
                # state["active_intent"] = "NONE"
                # Add prediction for requested slots.
                state["requested_slots"] = get_requested_slot(predictions=predictions[1], slots=service_schema.slots)
                # state["requested_slots"] = []

                # Add prediction for user goal (slot values).
                # Categorical slots.
                # cat_out_dict = set_cat_slot(predictions_status=predictions[2], predictions_value=predictions[3], cat_slots=service_schema.categorical_slots, cat_slot_values=service_schema.categorical_slot_values, sys_slots_agg=sys_slots_agg.get(frame["service"], None), cat_value_thresh=cat_value_thresh)
                cat_out_dict = set_cat_slot(
                    predictions_status=predictions[2],
                    predictions_value=predictions[3],
                    cat_slots=service_schema.categorical_slots,
                    cat_slot_values=service_schema.categorical_slot_values,
                    sys_slots_agg=None,
                    cat_value_thresh=cat_value_thresh,
                )
                for k, v in cat_out_dict.items():
                    slot_values[k] = v

                # # Non-categorical slots.
                noncat_out_dict = set_noncat_slot(
                    predictions_status=predictions[4],
                    predictions_value=predictions[5],
                    non_cat_slots=service_schema.non_categorical_slots,
                    user_utterance=system_user_utterance,
                    sys_slots_agg=sys_slots_agg.get(frame["service"], None),
                    non_cat_value_thresh=non_cat_value_thresh,
                )
                for k, v in noncat_out_dict.items():
                    slot_values[k] = v
                # Create a new dict to avoid overwriting the state in previous turns
                # because of use of same objects.
                state["slot_values"] = {s: [v] for s, v in slot_values.items()}
                frame["state"] = state
    return dialog


def get_predicted_intent(predictions, intents):
    """
    returns intent name with maximum score
    """
    assert len(predictions) == len(intents)
    active_intent_id = max(predictions, key=lambda k: predictions[k][0]['intent_status'])
    return intents[active_intent_id]


def get_requested_slot(predictions, slots):
    """
    returns list of slots which are predicted to be requested
    """
    active_indices = [k for k in predictions if predictions[k][0]["req_slot_status"] > REQ_SLOT_THRESHOLD]
    requested_slots = list(map(lambda k: slots[k], active_indices))
    return requested_slots


def write_predictions_to_file(
    predictions,
    input_json_files,
    output_dir,
    schemas,
    state_tracker,
    eval_debug,
    in_domain_services,
    cat_value_thresh,
    non_cat_value_thresh,
):
    """Write the predicted dialogues as json files.

  Args:
    predictions: An iterator containing model predictions. This is the output of
      the predict method in the estimator.
    input_json_files: A list of json paths containing the dialogues to run
      inference on.
    schemas: Schemas to all services in the dst dataset (train, dev and test splits).
    output_dir: The directory where output json files will be created.
  """
    logging.info(f"Writing predictions to {output_dir} started.")

    # Index all predictions.
    all_predictions = defaultdict(lambda: defaultdict(lambda: defaultdict(dict)))
    for idx, prediction in enumerate(predictions):
        eval_dataset, dialog_id, turn_id, service_name, model_task, slot_intent_id, value_id = prediction[
            'example_id'
        ].split('-')
        all_predictions[(dialog_id, turn_id, service_name)][int(model_task)][int(slot_intent_id)][
            int(value_id)
        ] = prediction
    logging.info(f'Predictions for {idx} examples in {eval_dataset} dataset are getting processed.')

    # Read each input file and write its predictions.
    for input_file_path in input_json_files:
        with open(input_file_path) as f:
            dialogs = json.load(f)
            logging.debug(f'{input_file_path} file is loaded')
            pred_dialogs = []
            for d in dialogs:
                pred_dialog = get_predicted_dialog(
                    d, all_predictions, schemas, state_tracker, cat_value_thresh, non_cat_value_thresh
                )
                pred_dialogs.append(pred_dialog)
            f.close()
        input_file_name = os.path.basename(input_file_path)
        output_file_path = os.path.join(output_dir, input_file_name)
        with open(output_file_path, "w") as f:
            json.dump(pred_dialogs, f, indent=2, separators=(",", ": "), sort_keys=True)
            f.close()