import csv
import logging

from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Exists, OuterRef, Prefetch
from django.utils import timezone
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _, gettext_lazy

from adjallocation.allocation import AdjudicatorAllocation
from adjallocation.models import DebateAdjudicator
from draw.models import Debate, DebateTeam
from importer.forms import ImportValidationError
from participants.models import Adjudicator, Speaker, Team
from registration.form_utils import CustomQuestionsFormMixin
from results.forms import TournamentPasswordField
from tournaments.models import Round

from .models import AdjudicatorBaseScoreHistory, AdjudicatorFeedback
from .utils import expected_feedback_targets

logger = logging.getLogger(__name__)

ADJUDICATOR_POSITION_NAMES = {
    AdjudicatorAllocation.POSITION_CHAIR: gettext_lazy("chair"),
    AdjudicatorAllocation.POSITION_ONLY: gettext_lazy("solo"),
    AdjudicatorAllocation.POSITION_PANELLIST: gettext_lazy("panellist"),
    AdjudicatorAllocation.POSITION_TRAINEE: gettext_lazy("trainee"),
}


# ==============================================================================
# General, but only used here
# ==============================================================================


class RequiredTypedChoiceField(forms.TypedChoiceField):
    def clean(self, value):
        value = super(RequiredTypedChoiceField, self).clean(value)
        if value == "None":
            # Translators: Please leave this blank, it should be left for the base Django translations.
            raise forms.ValidationError(_("This field is required."))
        return value


# ==============================================================================
# Feedback Forms
# ==============================================================================

class BaseFeedbackForm(CustomQuestionsFormMixin, forms.Form):
    """Base class for all dynamically-created feedback forms. Contains all
    question fields."""

    # parameters set at "compile time" by subclasses
    _tournament = None  # must be set by subclasses
    _use_tournament_password = False
    _confirm_on_submit = False
    _enforce_required = True
    _ignored_option = False
    question_filter = dict()

    def __init__(self, *args, **kwargs):
        super(BaseFeedbackForm, self).__init__(*args, **kwargs)
        self._create_fields()

    @staticmethod
    def coerce_target(value):
        debate_id, adj_id = value.split('-')
        debate = Debate.objects.get(id=int(debate_id))
        adjudicator = Adjudicator.objects.get(id=int(adj_id))
        return debate, adjudicator

    def get_custom_question_queryset(self):
        return self._tournament.adj_feedback_questions.filter(**self.question_filter)

    def _create_fields(self):
        """Creates dynamic fields in the form."""
        # Feedback questions defined for the tournament
        adj_min_score = self._tournament.pref('adj_min_score')
        adj_max_score = self._tournament.pref('adj_max_score')
        score_label = mark_safe(_("Overall score (%(min)d=worst; %(max)d=best)*") % {
                'min': int(adj_min_score), 'max': int(adj_max_score)})
        self.fields['score'] = forms.FloatField(min_value=adj_min_score, max_value=adj_max_score, label=score_label)

        self.add_question_fields()

        # Tournament password field, if applicable
        if self._use_tournament_password and self._tournament.pref('public_use_password'):
            self.fields['password'] = TournamentPasswordField(tournament=self._tournament)

        if self._ignored_option:
            self.fields['ignored'] = forms.BooleanField(label=_("Ignored"), required=False)

    def save_adjudicatorfeedback(self, **kwargs):
        """Saves the question fields and returns the AdjudicatorFeedback.
        To be called by save() of child classes."""
        af = AdjudicatorFeedback(**kwargs)

        af.confirmed = self._confirm_on_submit
        if af.confirmed:
            af.confirm_timestamp = timezone.now()
            af.confirmer = kwargs.get('submitter')
        af.score = self.cleaned_data['score']

        if self._ignored_option:
            af.ignored = self.cleaned_data['ignored']

        af.save()
        self.save_answers(af)

        return af


def make_feedback_form_class(source, tournament, *args, **kwargs):
    """Constructs a FeedbackForm class specific to the given source.
    'source' is the Adjudicator or Team who is giving feedback.
    'submission_fields' is a dict of fields that is passed directly as keyword
        arguments to Submission.
    'confirm_on_submit' is a bool, and indicates that this feedback should be
        as confirmed and all others discarded."""
    if isinstance(source, Adjudicator):
        return make_feedback_form_class_for_adj(source, tournament, *args, **kwargs)
    elif isinstance(source, Speaker):
        return make_feedback_form_class_for_team(source.team, tournament, *args, **kwargs)
    elif isinstance(source, Team):
        return make_feedback_form_class_for_team(source, tournament, *args, **kwargs)
    else:
        raise TypeError('source must be Adjudicator, Speaker, or Team: %r' % source)


def make_feedback_form_class_for_adj(source, tournament, submission_fields, confirm_on_submit=False,
                                     enforce_required=True, include_unreleased_draws=False,
                                     use_tournament_password=False, ignored_option=False):
    """Constructs a FeedbackForm class specific to the given source adjudicator.
    Parameters are as for make_feedback_form_class."""

    def adj_choice(adj, debate, pos):
        value = '%d-%d' % (debate.id, adj.id)
        # Translators: e.g. "Megan Pearson (chair)", with adjpos="chair"
        display = _("Submitted - ") if adj.submitted else ""
        display += _("%(name)s (%(adjpos)s)") % {'name': adj.get_public_name(tournament), 'adjpos': ADJUDICATOR_POSITION_NAMES[pos]}
        return value, display

    adjfeedback_query = AdjudicatorFeedback.objects.filter(
        source_adjudicator__adjudicator=source, source_adjudicator__debate=OuterRef('debate'),
        adjudicator=OuterRef('adjudicator'), confirmed=True,
    )
    debateadjs = DebateAdjudicator.objects.filter(
        debate__round__tournament=tournament, adjudicator=source,
        debate__round__seq__lte=tournament.current_round.seq,
        debate__round__stage=Round.Stage.PRELIMINARY,
    ).order_by('-debate__round__seq').select_related('debate__round').prefetch_related(
        Prefetch(
            'debate__debateadjudicator_set',
            queryset=DebateAdjudicator.objects.all().select_related('adjudicator').annotate(submitted=Exists(adjfeedback_query)),
        ),
    )

    if include_unreleased_draws:
        debateadjs = debateadjs.filter(debate__round__draw_status__in=[Round.Status.CONFIRMED, Round.Status.RELEASED])
    else:
        debateadjs = debateadjs.filter(debate__round__draw_status=Round.Status.RELEASED)

    choices = [(None, _("-- Adjudicators --"))]
    for debateadj in debateadjs:
        targets = expected_feedback_targets(debateadj, tournament.pref('feedback_paths'))
        round_choices = []
        for target, pos in targets:
            round_choices.append(adj_choice(target, debateadj.debate, pos))
        choices.append((debateadj.debate.round.name, round_choices))

    class FeedbackForm(BaseFeedbackForm):
        _tournament = tournament  # BaseFeedbackForm setting
        _use_tournament_password = use_tournament_password  # BaseFeedbackForm setting
        _confirm_on_submit = confirm_on_submit
        _enforce_required = enforce_required
        _ignored_option = ignored_option
        question_filter = dict(from_adj=True)

        target = RequiredTypedChoiceField(choices=choices, coerce=BaseFeedbackForm.coerce_target,
                label=_("Adjudicator this feedback is about"))

        def save(self):
            """Saves the form and returns the AdjudicatorFeedback object."""
            debate, target = self.cleaned_data['target']
            sa = DebateAdjudicator.objects.get(adjudicator=source, debate=debate)
            kwargs = dict(adjudicator=target, source_adjudicator=sa, source_team=None)
            kwargs.update(submission_fields)
            return self.save_adjudicatorfeedback(**kwargs)

    return FeedbackForm


def make_feedback_form_class_for_team(source, tournament, submission_fields, confirm_on_submit=False,
                                      enforce_required=True, include_unreleased_draws=False,
                                      use_tournament_password=False, ignored_option=False):
    """Constructs a FeedbackForm class specific to the given source team.
    Parameters are as for make_feedback_form_class."""

    def adj_choice(adj, debate, pos):
        value = '%d-%d' % (debate.id, adj.id)

        display = _("Submitted - ") if adj.submitted else ""
        if pos == AdjudicatorAllocation.POSITION_ONLY:
            display += _("%(name)s")
        elif tournament.pref('feedback_from_teams') == 'all-adjs':
            # Translators: e.g. "Megan Pearson (panellist)", with round="Round 3", adjpos="panellist"
            display += _("%(name)s (%(adjpos)s)")
        elif pos == AdjudicatorAllocation.POSITION_CHAIR:
            # feedback expected only on orallist
            display += _("%(name)s (chair gave oral)")
        else:
            display += _("%(name)s (panellist gave oral as chair rolled)")

        display %= {'name': adj.get_public_name(tournament), 'adjpos': ADJUDICATOR_POSITION_NAMES[pos]}
        return value, display

    # Only include non-silent rounds for teams.
    debates = Debate.objects.filter(
        debateteam__team=source, round__silent=False,
        round__seq__lte=tournament.current_round.seq,
        round__stage=Round.Stage.PRELIMINARY,
    ).order_by('-round__seq').prefetch_related(Prefetch(
        'debateadjudicator_set',
        queryset=DebateAdjudicator.objects.all().select_related('adjudicator').annotate(submitted=Exists(
            AdjudicatorFeedback.objects.filter(
                source_team__team=source, source_team__debate=OuterRef('debate'),
                adjudicator=OuterRef('adjudicator'), confirmed=True,
            ),
        )),
    ))

    if include_unreleased_draws:
        debates = debates.filter(round__draw_status__in=[Round.Status.CONFIRMED, Round.Status.RELEASED])
    else:
        debates = debates.filter(round__draw_status=Round.Status.RELEASED)

    choices = [(None, _("-- Adjudicators --"))]
    for debate in debates:
        # Need to associate the submission status to Adjudicator objects
        # so that they pass to the AdjudicatorAllocation
        for da in debate.debateadjudicator_set.all():
            da.adjudicator.submitted = da.submitted
        if tournament.pref('feedback_from_teams') == 'all-adjs':
            das = debate.adjudicators.with_positions()
        elif tournament.pref('feedback_from_teams') == 'orallist':
            das = debate.adjudicators.voting_with_positions()
        else:
            das = []

        round_choices = []
        for adj, pos in das:
            round_choices.append(adj_choice(adj, debate, pos))
        choices.append((debate.round.name, round_choices))

    class FeedbackForm(BaseFeedbackForm):
        _tournament = tournament  # BaseFeedbackForm setting
        _use_tournament_password = use_tournament_password  # BaseFeedbackForm setting
        _confirm_on_submit = confirm_on_submit
        _enforce_required = enforce_required
        _ignored_option = ignored_option
        question_filter = dict(from_team=True)

        target = RequiredTypedChoiceField(choices=choices, coerce=BaseFeedbackForm.coerce_target)

        def save(self):
            # Saves the form and returns the m.AdjudicatorFeedback object
            debate, target = self.cleaned_data['target']
            st = DebateTeam.objects.get(team=source, debate=debate)
            kwargs = dict(adjudicator=target, source_adjudicator=None, source_team=st)
            kwargs.update(submission_fields)
            return self.save_adjudicatorfeedback(**kwargs)

    return FeedbackForm


# ==============================================================================
# Update adjudicator scores in bulk
# ==============================================================================

class UpdateAdjudicatorScoresForm(forms.Form):
    """Form that takes in a CSV-style list of adjudicators with scores, and
    saves the scores, overwriting existing ones. Unlike the other forms, this
    isn't part of a wizard, it just saves directly."""

    scores_raw = forms.CharField(widget=forms.Textarea(attrs={'rows': 20}))

    def __init__(self, tournament, *args, **kwargs):
        self.tournament = tournament
        return super().__init__(*args, **kwargs)

    def clean_scores_raw(self):
        lines = self.cleaned_data['scores_raw'].split('\n')
        errors = []
        records = []

        logger.info("UpdateAdjudicatorScoresForm: Cleaning started (1 of 5)")

        for i, line in enumerate(csv.reader(lines), start=1):
            errors_in_line = []

            if len(line) < 1:
                continue # skip blank lines
            if len(line) < 2:
                errors_in_line.append(ImportValidationError(i,
                    _("This line (for %(adjudicator)s) didn't have a score") %
                    {'adjudicator': line[0]}))
                continue
            if len(line) > 2:
                errors_in_line.append(ImportValidationError(i,
                    _("This line (for %(adjudicator)s) had too many columns") %
                    {'adjudicator': line[0]}))

            name, score = [x.strip() for x in line[:2]]

            try:
                try:
                    name = int(name)
                    adj = self.tournament.relevant_adjudicators.get(id=name)
                except ValueError:
                    adj = self.tournament.relevant_adjudicators.get(name=name)
            except Adjudicator.MultipleObjectsReturned:
                errors_in_line.append(ImportValidationError(i,
                    _("There are several adjudicators called \"%(adjudicator)s\", so "
                      "you can't use the bulk importer to update their score. "
                      "Please do so in the Feedback Overview page instead.") %
                    {'adjudicator': name}))
            except Adjudicator.DoesNotExist:
                errors_in_line.append(ImportValidationError(i,
                    _("There is no adjudicator in this tournament with the "
                      "name \"%(adjudicator)s\"") %
                    {'adjudicator': name}))

            try:
                score = float(score)
            except ValueError:
                errors_in_line.append(ImportValidationError(i,
                    _("The score for %(adjudicator)s, \"%(score)s\", isn't a number") %
                    {'adjudicator': name, 'score': score}))

            if errors_in_line:
                errors.extend(errors_in_line)
                continue

            records.append((adj, score))

        logger.info("UpdateAdjudicatorScoresForm: Cleaning done (2 of 5)")

        if errors:
            raise ValidationError(errors)

        if len(records) == 0:
            raise ValidationError(_("There were no scores to import."))

        return records

    def save(self):
        records = self.cleaned_data.get('scores_raw', [])
        history_instances = []

        logger.info("UpdateAdjudicatorScoresForm: Saving to database started (3 of 5)")

        for adj, score in records:
            adj.base_score = score
            adj.save()

            history_instances.append(AdjudicatorBaseScoreHistory(
                adjudicator=adj,
                round=self.tournament.current_round,
                score=score,
            ))

        logger.info("UpdateAdjudicatorScoresForm: Saving scores to database done (4 of 5)")

        AdjudicatorBaseScoreHistory.objects.bulk_create(history_instances)

        logger.info("UpdateAdjudicatorScoresForm: Saving base score histories to database done (5 of 5)")

        return len(records)
