"""Wrapper to convert a openspiel environment into a pettingzoo compatible environment."""

import functools

import numpy as np
import pettingzoo as pz
import pyspiel
from gymnasium import spaces
from gymnasium.utils import seeding


class OpenspielWrapper(pz.AECEnv):
    """Wrapper that converts a openspiel environment into a pettingzoo environment."""

    metadata = {"render_modes": [None]}

    def __init__(
        self,
        game: pyspiel.Game,
        render_mode: None,
    ):
        self.game = game
        self.possible_agents = [
            "player_" + str(r) for r in range(self.game.num_players())
        ]
        self.agent_id_name_mapping = dict(
            zip(range(self.game.num_players()), self.possible_agents)
        )
        self.agent_name_id_mapping = dict(
            zip(self.possible_agents, range(self.game.num_players()))
        )

        self.render_mode = render_mode

    @functools.lru_cache(maxsize=None)
    def observation_space(self, agent):
        try:
            return spaces.Box(
                low=-np.inf, high=np.inf, shape=self.game.observation_tensor_shape()
            )
        except pyspiel.SpielError as e:
            raise NotImplementedError(f"{str(e)[:-1]} for {self.game}.")

    @functools.lru_cache(maxsize=None)
    def action_space(self, agent):
        try:
            return spaces.Discrete(self.game.num_distinct_actions())
        except pyspiel.SpielError as e:
            raise NotImplementedError(f"{str(e)[:-1]} for {self.game}.")

    def render(self):
        raise NotImplementedError("No render available for openspiel.")

    def observe(self, agent):
        return np.array(self.observations[agent])

    def close(self):
        pass

    def reset(self, seed=None, return_info=False, options=None):
        # initialize the seed
        self.np_random, seed = seeding.np_random(seed)

        # all agents
        self.agents = self.possible_agents[:]

        # boilerplate stuff
        self._cumulative_rewards = {a: 0 for a in self.agents}
        self.rewards = {a: 0 for a in self.agents}
        self.terminations = {a: False for a in self.agents}
        self.truncations = {a: False for a in self.agents}
        self.infos = {a: {} for a in self.agents}

        # get a new game state, game_length = number of game nodes
        self.game_length = 1
        self.game_state = self.game.new_initial_state()

        # holders in case of simultaneous actions
        self.simultaneous_actions = dict()

        # step through chance nodes, then update obs and act masks
        self._execute_chance_node()
        self._update_observations()
        self._update_action_masks()

        # get the current agent and update all action masks
        self.agent_selection = self.agent_id_name_mapping[
            self.game_state.current_player()
        ]

    def _execute_chance_node(self):
        # if the game state is a chance node, choose a random outcome
        while self.game_state.is_chance_node():
            self.game_length += 1
            outcomes_with_probs = self.game_state.chance_outcomes()
            action_list, prob_list = zip(*outcomes_with_probs)
            action = self.np_random.choice(action_list, p=prob_list)
            self.game_state.apply_action(action)

    def _execute_action_node(self, action):
        # if the game state is a simultaneous node, we need to collect all actions first
        if self.game_state.is_simultaneous_node():
            # store the agent's action
            self.simultaneous_actions[self.agent_selection] = action

            # set the agents reward to 0 since it's seen it
            self._cumulative_rewards[self.agent_selection] = 0

            # find agents for whom we don't have actions yet and get its action
            for agent in self.agents:
                if agent not in self.simultaneous_actions:
                    self.agent_selection = agent
                    return

            # if we already have all the actions, just step regularly
            self.game_state.apply_action(self.simultaneous_actions.values())
            self.game_length += 1

            # clear the simultaneous actions holder
            self.simultaneous_actions = dict()
        else:
            # if not simultaneous, step the state generically
            self.game_state.apply_action(action)
            self.game_length += 1

            # select the next agent depending on the type of agent
            current_player = self.game_state.current_player()
            if current_player >= 0:
                self.agent_selection = self.agent_id_name_mapping[current_player]
            else:
                self.agent_selection = self.agents[0]

    def _update_observations(self):
        try:
            self.observations = {
                a: self.game_state.observation_tensor(self.agent_name_id_mapping[a])
                for a in self.agents
            }
        except pyspiel.SpielError as e:
            raise NotImplementedError(f"{str(e)[:-1]} for {self.game}.")

    def _update_action_masks(self):
        for agent_id in range(self.game.num_players()):
            agent_name = self.agent_id_name_mapping[agent_id]
            action_mask = np.zeros(self.action_space(agent_name).n, dtype=np.int8)
            action_mask[self.game_state.legal_actions(agent_id)] = 1
            self.infos[agent_name] = {"action_mask": action_mask}

    def _update_rewards(self):
        # update cumulative rewards
        rewards = self.game_state.rewards()
        self._cumulative_rewards = {
            self.agent_id_name_mapping[id]: rewards[id]
            for id in range(self.game.num_players())
        }

    def _end_routine(self):
        # special function to deal with ending steps
        # in openspiel, all agents end together so we need to
        # treat it as so

        self.terminations = {a: False for a in self.agents}
        # check for terminal
        if self.game_state.is_terminal():
            self.terminations = {a: True for a in self.agents}

        # check for truncation
        self.truncations = {a: False for a in self.agents}
        if self.game_length > self.game.max_game_length():
            self.truncations = {a: True for a in self.agents}

        # check for action masks because openspiel doesn't do it themselves
        for agent in self.agents:
            if np.sum(self.infos[agent]["action_mask"]) == 0:
                self.terminations = {a: True for a in self.agents}

        # if terminal, start deleting agents
        if (
            self.terminations[self.agent_selection]
            or self.truncations[self.agent_selection]
        ):
            self.agents.remove(self.agent_selection)
            if self.agents:
                self.agent_selection = self.agents[0]

            return True

        return False

    def step(self, action):
        # handle the possibility of an end step
        if self._end_routine():
            return
        else:
            # step the environment
            self._execute_action_node(action)
            self._execute_chance_node()
            self._update_observations()
            self._update_action_masks()
            self._update_rewards()
