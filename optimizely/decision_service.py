# Copyright 2017-2022, Optimizely
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations
from typing import TYPE_CHECKING, NamedTuple, Optional, Sequence

from . import bucketer
from . import entities
from .decision.optimizely_decide_option import OptimizelyDecideOption
from .helpers import audience as audience_helper
from .helpers import enums
from .helpers import experiment as experiment_helper
from .helpers import validator
from .optimizely_user_context import OptimizelyUserContext, UserAttributes
from .user_profile import UserProfile, UserProfileService, UserProfileTracker

if TYPE_CHECKING:
    # prevent circular dependenacy by skipping import at runtime
    from .project_config import ProjectConfig
    from .logger import Logger


class Decision(NamedTuple):
    """Named tuple containing selected experiment, variation and source.
    None if no experiment/variation was selected."""
    experiment: Optional[entities.Experiment]
    variation: Optional[entities.Variation]
    source: Optional[str]


class DecisionService:
    """ Class encapsulating all decision related capabilities. """

    def __init__(self, logger: Logger, user_profile_service: Optional[UserProfileService]):
        self.bucketer = bucketer.Bucketer()
        self.logger = logger
        self.user_profile_service = user_profile_service

        # Map of user IDs to another map of experiments to variations.
        # This contains all the forced variations set by the user
        # by calling set_forced_variation (it is not the same as the
        # whitelisting forcedVariations data structure).
        self.forced_variation_map: dict[str, dict[str, str]] = {}

    def _get_bucketing_id(self, user_id: str, attributes: Optional[UserAttributes]) -> tuple[str, list[str]]:
        """ Helper method to determine bucketing ID for the user.

        Args:
          user_id: ID for user.
          attributes: Dict representing user attributes. May consist of bucketing ID to be used.

        Returns:
          String representing bucketing ID if it is a String type in attributes else return user ID
          array of log messages representing decision making.
        """
        decide_reasons: list[str] = []
        attributes = attributes or UserAttributes({})
        bucketing_id = attributes.get(enums.ControlAttributes.BUCKETING_ID)

        if bucketing_id is not None:
            if isinstance(bucketing_id, str):
                return bucketing_id, decide_reasons
            message = 'Bucketing ID attribute is not a string. Defaulted to user_id.'
            self.logger.warning(message)
            decide_reasons.append(message)

        return user_id, decide_reasons

    def set_forced_variation(
        self, project_config: ProjectConfig, experiment_key: str,
        user_id: str, variation_key: Optional[str]
    ) -> bool:
        """ Sets users to a map of experiments to forced variations.

          Args:
            project_config: Instance of ProjectConfig.
            experiment_key: Key for experiment.
            user_id: The user ID.
            variation_key: Key for variation. If None, then clear the existing experiment-to-variation mapping.

          Returns:
            A boolean value that indicates if the set completed successfully.
        """
        experiment = project_config.get_experiment_from_key(experiment_key)
        if not experiment:
            # The invalid experiment key will be logged inside this call.
            return False

        experiment_id = experiment.id
        if variation_key is None:
            if user_id in self.forced_variation_map:
                experiment_to_variation_map = self.forced_variation_map[user_id]
                if experiment_id in experiment_to_variation_map:
                    del self.forced_variation_map[user_id][experiment_id]
                    self.logger.debug(
                        f'Variation mapped to experiment "{experiment_key}" has been removed for user "{user_id}".'
                    )
                else:
                    self.logger.debug(
                        f'Nothing to remove. Variation mapped to experiment "{experiment_key}" for '
                        f'user "{user_id}" does not exist.'
                    )
            else:
                self.logger.debug(f'Nothing to remove. User "{user_id}" does not exist in the forced variation map.')
            return True

        if not validator.is_non_empty_string(variation_key):
            self.logger.debug('Variation key is invalid.')
            return False

        forced_variation = project_config.get_variation_from_key(experiment_key, variation_key)
        if not forced_variation:
            # The invalid variation key will be logged inside this call.
            return False

        variation_id = forced_variation.id

        if user_id not in self.forced_variation_map:
            self.forced_variation_map[user_id] = {experiment_id: variation_id}
        else:
            self.forced_variation_map[user_id][experiment_id] = variation_id

        self.logger.debug(
            f'Set variation "{variation_id}" for experiment "{experiment_id}" and '
            f'user "{user_id}" in the forced variation map.'
        )
        return True

    def get_forced_variation(
        self, project_config: ProjectConfig, experiment_key: str, user_id: str
    ) -> tuple[Optional[entities.Variation], list[str]]:
        """ Gets the forced variation key for the given user and experiment.

          Args:
            project_config: Instance of ProjectConfig.
            experiment_key: Key for experiment.
            user_id: The user ID.

          Returns:
            The variation which the given user and experiment should be forced into and
             array of log messages representing decision making.
        """
        decide_reasons: list[str] = []
        if user_id not in self.forced_variation_map:
            message = f'User "{user_id}" is not in the forced variation map.'
            self.logger.debug(message)
            return None, decide_reasons

        experiment = project_config.get_experiment_from_key(experiment_key)
        if not experiment:
            # The invalid experiment key will be logged inside this call.
            return None, decide_reasons

        experiment_to_variation_map = self.forced_variation_map.get(user_id)

        if not experiment_to_variation_map:
            message = f'No experiment "{experiment_key}" mapped to user "{user_id}" in the forced variation map.'
            self.logger.debug(message)
            return None, decide_reasons

        variation_id = experiment_to_variation_map.get(experiment.id)
        if variation_id is None:
            message = f'No variation mapped to experiment "{experiment_key}" in the forced variation map.'
            self.logger.debug(message)
            return None, decide_reasons

        variation = project_config.get_variation_from_id(experiment_key, variation_id)
        # this case is logged in get_variation_from_id
        if variation is None:
            return None, decide_reasons

        message = f'Variation "{variation.key}" is mapped to experiment "{experiment_key}" and ' \
                  f'user "{user_id}" in the forced variation map'
        self.logger.debug(message)
        decide_reasons.append(message)
        return variation, decide_reasons

    def get_whitelisted_variation(
        self, project_config: ProjectConfig, experiment: entities.Experiment, user_id: str
    ) -> tuple[Optional[entities.Variation], list[str]]:
        """ Determine if a user is forced into a variation (through whitelisting)
        for the given experiment and return that variation.

        Args:
          project_config: Instance of ProjectConfig.
          experiment: Object representing the experiment for which user is to be bucketed.
          user_id: ID for the user.

        Returns:
          Variation in which the user with ID user_id is forced into. None if no variation and
           array of log messages representing decision making.
        """
        decide_reasons = []
        forced_variations = experiment.forcedVariations

        if forced_variations and user_id in forced_variations:
            forced_variation_key = forced_variations[user_id]
            forced_variation = project_config.get_variation_from_key(experiment.key, forced_variation_key)

            if forced_variation:
                message = f'User "{user_id}" is forced in variation "{forced_variation_key}".'
                self.logger.info(message)
                decide_reasons.append(message)

            return forced_variation, decide_reasons

        return None, decide_reasons

    def get_stored_variation(
        self, project_config: ProjectConfig, experiment: entities.Experiment, user_profile: UserProfile
    ) -> Optional[entities.Variation]:
        """ Determine if the user has a stored variation available for the given experiment and return that.

        Args:
          project_config: Instance of ProjectConfig.
          experiment: Object representing the experiment for which user is to be bucketed.
          user_profile: UserProfile object representing the user's profile.

        Returns:
          Variation if available. None otherwise.
        """
        user_id = user_profile.user_id
        variation_id = user_profile.get_variation_for_experiment(experiment.id)

        if variation_id:
            variation = project_config.get_variation_from_id(experiment.key, variation_id)
            if variation:
                message = f'Found a stored decision. User "{user_id}" is in ' \
                          f'variation "{variation.key}" of experiment "{experiment.key}".'
                self.logger.info(message)
                return variation

        return None

    def get_variation(
        self,
        project_config: ProjectConfig,
        experiment: entities.Experiment,
        user_context: OptimizelyUserContext,
        user_profile_tracker: Optional[UserProfileTracker],
        reasons: list[str] = [],
        options: Optional[Sequence[str]] = None
    ) -> tuple[Optional[entities.Variation], list[str]]:
        """ Top-level function to help determine variation user should be put in.

        First, check if experiment is running.
        Second, check if user is forced in a variation.
        Third, check if there is a stored decision for the user and return the corresponding variation.
        Fourth, figure out if user is in the experiment by evaluating audience conditions if any.
        Fifth, bucket the user and return the variation.

        Args:
          project_config: Instance of ProjectConfig.
          experiment: Experiment for which user variation needs to be determined.
          user_context: contains user id and attributes.
          user_profile_tracker: tracker for reading and updating user profile of the user.
          reasons: Decision reasons.
          options: Decide options.

        Returns:
          Variation user should see. None if user is not in experiment or experiment is not running
          And an array of log messages representing decision making.
        """
        user_id = user_context.user_id

        if options:
            ignore_user_profile = OptimizelyDecideOption.IGNORE_USER_PROFILE_SERVICE in options
        else:
            ignore_user_profile = False

        decide_reasons = []
        if reasons is not None:
            decide_reasons += reasons
        # Check if experiment is running
        if not experiment_helper.is_experiment_running(experiment):
            message = f'Experiment "{experiment.key}" is not running.'
            self.logger.info(message)
            decide_reasons.append(message)
            return None, decide_reasons

        # Check if the user is forced into a variation
        variation: Optional[entities.Variation]
        variation, reasons_received = self.get_forced_variation(project_config, experiment.key, user_id)
        decide_reasons += reasons_received
        if variation:
            return variation, decide_reasons

        # Check to see if user is white-listed for a certain variation
        variation, reasons_received = self.get_whitelisted_variation(project_config, experiment, user_id)
        decide_reasons += reasons_received
        if variation:
            return variation, decide_reasons

        # Check to see if user has a decision available for the given experiment
        if user_profile_tracker is not None and not ignore_user_profile:
            variation = self.get_stored_variation(project_config, experiment, user_profile_tracker.get_user_profile())
            if variation:
                message = f'Returning previously activated variation ID "{variation}" of experiment ' \
                          f'"{experiment}" for user "{user_id}" from user profile.'
                self.logger.info(message)
                decide_reasons.append(message)
                return variation, decide_reasons
            else:
                self.logger.warning('User profile has invalid format.')

        # Bucket user and store the new decision
        audience_conditions = experiment.get_audience_conditions_or_ids()
        user_meets_audience_conditions, reasons_received = audience_helper.does_user_meet_audience_conditions(
            project_config, audience_conditions,
            enums.ExperimentAudienceEvaluationLogs,
            experiment.key,
            user_context, self.logger)
        decide_reasons += reasons_received
        if not user_meets_audience_conditions:
            message = f'User "{user_id}" does not meet conditions to be in experiment "{experiment.key}".'
            self.logger.info(message)
            decide_reasons.append(message)
            return None, decide_reasons

        # Determine bucketing ID to be used
        bucketing_id, bucketing_id_reasons = self._get_bucketing_id(user_id, user_context.get_user_attributes())
        decide_reasons += bucketing_id_reasons
        variation, bucket_reasons = self.bucketer.bucket(project_config, experiment, user_id, bucketing_id)
        decide_reasons += bucket_reasons
        if isinstance(variation, entities.Variation):
            message = f'User "{user_id}" is in variation "{variation.key}" of experiment {experiment.key}.'
            self.logger.info(message)
            decide_reasons.append(message)
            # Store this new decision and return the variation for the user
            if user_profile_tracker is not None and not ignore_user_profile:
                try:
                    user_profile_tracker.update_user_profile(experiment, variation)
                except:
                    self.logger.exception(f'Unable to save user profile for user "{user_id}".')
            return variation, decide_reasons
        message = f'User "{user_id}" is in no variation.'
        self.logger.info(message)
        decide_reasons.append(message)
        return None, decide_reasons

    def get_variation_for_rollout(
        self, project_config: ProjectConfig, feature: entities.FeatureFlag, user_context: OptimizelyUserContext
    ) -> tuple[Decision, list[str]]:
        """ Determine which experiment/variation the user is in for a given rollout.
            Returns the variation of the first experiment the user qualifies for.

        Args:
          project_config: Instance of ProjectConfig.
          flagKey: Feature key.
          rollout: Rollout for which we are getting the variation.
          user: ID and attributes for user.
          options: Decide options.

        Returns:
          Decision namedtuple consisting of experiment and variation for the user and
          array of log messages representing decision making.
        """
        decide_reasons: list[str] = []
        user_id = user_context.user_id
        attributes = user_context.get_user_attributes()

        if not feature or not feature.rolloutId:
            return Decision(None, None, enums.DecisionSources.ROLLOUT), decide_reasons

        rollout = project_config.get_rollout_from_id(feature.rolloutId)

        if not rollout:
            message = f'There is no rollout of feature {feature.key}.'
            self.logger.debug(message)
            decide_reasons.append(message)
            return Decision(None, None, enums.DecisionSources.ROLLOUT), decide_reasons

        rollout_rules = project_config.get_rollout_experiments(rollout)

        if not rollout_rules:
            message = f'Rollout {rollout.id} has no experiments.'
            self.logger.debug(message)
            decide_reasons.append(message)
            return Decision(None, None, enums.DecisionSources.ROLLOUT), decide_reasons

        index = 0
        while index < len(rollout_rules):
            skip_to_everyone_else = False

            # check forced decision first
            rule = rollout_rules[index]
            optimizely_decision_context = OptimizelyUserContext.OptimizelyDecisionContext(feature.key, rule.key)
            forced_decision_variation, reasons_received = self.validated_forced_decision(
                project_config, optimizely_decision_context, user_context)
            decide_reasons += reasons_received

            if forced_decision_variation:
                return Decision(experiment=rule, variation=forced_decision_variation,
                                source=enums.DecisionSources.ROLLOUT), decide_reasons

            bucketing_id, bucket_reasons = self._get_bucketing_id(user_id, attributes)
            decide_reasons += bucket_reasons

            everyone_else = (index == len(rollout_rules) - 1)
            logging_key = "Everyone Else" if everyone_else else str(index + 1)

            rollout_rule = project_config.get_experiment_from_id(rule.id)
            # error is logged in get_experiment_from_id
            if rollout_rule is None:
                continue
            audience_conditions = rollout_rule.get_audience_conditions_or_ids()

            audience_decision_response, reasons_received_audience = audience_helper.does_user_meet_audience_conditions(
                project_config, audience_conditions, enums.RolloutRuleAudienceEvaluationLogs,
                logging_key, user_context, self.logger)

            decide_reasons += reasons_received_audience

            if audience_decision_response:
                message = f'User "{user_id}" meets audience conditions for targeting rule {logging_key}.'
                self.logger.debug(message)
                decide_reasons.append(message)

                bucketed_variation, bucket_reasons = self.bucketer.bucket(project_config, rollout_rule, user_id,
                                                                          bucketing_id)
                decide_reasons.extend(bucket_reasons)

                if bucketed_variation:
                    message = f'User "{user_id}" bucketed into a targeting rule {logging_key}.'
                    self.logger.debug(message)
                    decide_reasons.append(message)
                    return Decision(experiment=rule, variation=bucketed_variation,
                                    source=enums.DecisionSources.ROLLOUT), decide_reasons

                elif not everyone_else:
                    # skip this logging for EveryoneElse since this has a message not for everyone_else
                    message = f'User "{user_id}" not bucketed into a targeting rule {logging_key}. ' \
                              'Checking "Everyone Else" rule now.'
                    self.logger.debug(message)
                    decide_reasons.append(message)

                    # skip the rest of rollout rules to the everyone-else rule if audience matches but not bucketed.
                    skip_to_everyone_else = True

            else:
                message = f'User "{user_id}" does not meet audience conditions for targeting rule {logging_key}.'
                self.logger.debug(message)
                decide_reasons.append(message)

            # the last rule is special for "Everyone Else"
            index = len(rollout_rules) - 1 if skip_to_everyone_else else index + 1

        return Decision(None, None, enums.DecisionSources.ROLLOUT), decide_reasons

    def get_variation_for_feature(
        self,
        project_config: ProjectConfig,
        feature: entities.FeatureFlag,
        user_context: OptimizelyUserContext,
        options: Optional[list[str]] = None
    ) -> tuple[Decision, list[str]]:
        """ Returns the experiment/variation the user is bucketed in for the given feature.

        Args:
          project_config: Instance of ProjectConfig.
          feature: Feature for which we are determining if it is enabled or not for the given user.
          user_context: user context for user.
          options: Decide options.

        Returns:
          Decision namedtuple consisting of experiment and variation for the user.
    """
        return self.get_variations_for_feature_list(project_config, [feature], user_context, options)[0]

    def validated_forced_decision(
        self,
        project_config: ProjectConfig,
        decision_context: OptimizelyUserContext.OptimizelyDecisionContext,
        user_context: OptimizelyUserContext
    ) -> tuple[Optional[entities.Variation], list[str]]:
        """
        Gets forced decisions based on flag key, rule key and variation.

        Args:
            project_config: a project config
            decision context: a decision context
            user_context context: a user context

        Returns:
            Variation of the forced decision.
        """
        reasons: list[str] = []

        forced_decision = user_context.get_forced_decision(decision_context)

        flag_key = decision_context.flag_key
        rule_key = decision_context.rule_key

        if forced_decision:
            if not project_config:
                return None, reasons
            variation = project_config.get_flag_variation(flag_key, 'key', forced_decision.variation_key)
            if variation:
                if rule_key:
                    user_has_forced_decision = enums.ForcedDecisionLogs \
                        .USER_HAS_FORCED_DECISION_WITH_RULE_SPECIFIED.format(forced_decision.variation_key,
                                                                             flag_key,
                                                                             rule_key,
                                                                             user_context.user_id)

                else:
                    user_has_forced_decision = enums.ForcedDecisionLogs \
                        .USER_HAS_FORCED_DECISION_WITHOUT_RULE_SPECIFIED.format(forced_decision.variation_key,
                                                                                flag_key,
                                                                                user_context.user_id)

                reasons.append(user_has_forced_decision)
                user_context.logger.info(user_has_forced_decision)

                return variation, reasons

            else:
                if rule_key:
                    user_has_forced_decision_but_invalid = enums.ForcedDecisionLogs \
                        .USER_HAS_FORCED_DECISION_WITH_RULE_SPECIFIED_BUT_INVALID.format(flag_key,
                                                                                         rule_key,
                                                                                         user_context.user_id)
                else:
                    user_has_forced_decision_but_invalid = enums.ForcedDecisionLogs \
                        .USER_HAS_FORCED_DECISION_WITHOUT_RULE_SPECIFIED_BUT_INVALID.format(flag_key,
                                                                                            user_context.user_id)

                reasons.append(user_has_forced_decision_but_invalid)
                user_context.logger.info(user_has_forced_decision_but_invalid)

        return None, reasons

    def get_variations_for_feature_list(
        self,
        project_config: ProjectConfig,
        features: list[entities.FeatureFlag],
        user_context: OptimizelyUserContext,
        options: Optional[Sequence[str]] = None
    ) -> list[tuple[Decision, list[str]]]:
        """
        Returns the list of experiment/variation the user is bucketed in for the given list of features.
        Args:
                project_config: Instance of ProjectConfig.
                features: List of features for which we are determining if it is enabled or not for the given user.
                user_context: user context for user.
                options: Decide options.

        Returns:
            List of Decision namedtuple consisting of experiment and variation for the user.
        """
        decide_reasons: list[str] = []

        if options:
            ignore_ups = OptimizelyDecideOption.IGNORE_USER_PROFILE_SERVICE in options
        else:
            ignore_ups = False

        user_profile_tracker: Optional[UserProfileTracker] = None
        if self.user_profile_service is not None and not ignore_ups:
            user_profile_tracker = UserProfileTracker(user_context.user_id, self.user_profile_service, self.logger)
            user_profile_tracker.load_user_profile(decide_reasons, None)

        decisions = []

        for feature in features:
            feature_reasons = decide_reasons.copy()
            experiment_decision_found = False  # Track if an experiment decision was made for the feature

            # Check if the feature flag is under an experiment
            if feature.experimentIds:
                for experiment_id in feature.experimentIds:
                    experiment = project_config.get_experiment_from_id(experiment_id)
                    decision_variation = None

                    if experiment:
                        optimizely_decision_context = OptimizelyUserContext.OptimizelyDecisionContext(
                            feature.key, experiment.key)
                        forced_decision_variation, reasons_received = self.validated_forced_decision(
                            project_config, optimizely_decision_context, user_context)
                        feature_reasons.extend(reasons_received)

                        if forced_decision_variation:
                            decision_variation = forced_decision_variation
                        else:
                            decision_variation, variation_reasons = self.get_variation(
                                project_config, experiment, user_context, user_profile_tracker, feature_reasons, options
                            )
                            feature_reasons.extend(variation_reasons)

                        if decision_variation:
                            self.logger.debug(
                                f'User "{user_context.user_id}" '
                                f'bucketed into experiment "{experiment.key}" of feature "{feature.key}".'
                            )
                            decision = Decision(experiment, decision_variation, enums.DecisionSources.FEATURE_TEST)
                            decisions.append((decision, feature_reasons))
                            experiment_decision_found = True  # Mark that a decision was found
                            break  # Stop after the first successful experiment decision

            # Only process rollout if no experiment decision was found
            if not experiment_decision_found:
                rollout_decision, rollout_reasons = self.get_variation_for_rollout(project_config,
                                                                                   feature,
                                                                                   user_context)
                if rollout_reasons:
                    feature_reasons.extend(rollout_reasons)
                if rollout_decision:
                    self.logger.debug(f'User "{user_context.user_id}" '
                                      f'bucketed into rollout for feature "{feature.key}".')
                else:
                    self.logger.debug(f'User "{user_context.user_id}" '
                                      f'not bucketed into any rollout for feature "{feature.key}".')

                decisions.append((rollout_decision, feature_reasons))

        if self.user_profile_service is not None and user_profile_tracker is not None and ignore_ups is False:
            user_profile_tracker.save_user_profile()

        return decisions
