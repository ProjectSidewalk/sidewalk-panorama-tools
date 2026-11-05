"""sidewalk_panorama_tools.labels: vote arithmetic with every policy explicit (#222 step 4, docs/geometry.md).

The fixture is 14 anonymised features of a live Seattle rawLabels response (tests/fixtures/
rawlabels_validations_seattle.json, provenance inside it): it pins the field names, and it pins two facts about
what the API serves - its counts include the AI validator's votes, and its `correct` is the tie-as-null
majority - against the functions here.
"""

import json
import os

import pytest

from sidewalk_panorama_tools import labels as L
from sidewalk_panorama_tools.labels import Verdict, VoteCounts

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'rawlabels_validations_seattle.json')


def vote(value, kind='Human'):
    return {'validation': value, 'validator_type': kind, 'user_id': 'not-read'}


@pytest.fixture(scope='module')
def features():
    with open(FIXTURE, encoding='utf-8') as f:
        return json.load(f)['features']


class TestLabelUid:

    @pytest.mark.parametrize('label_id', [12327, '12327', ' 12327', 12327.0])
    def test_city_and_id(self, label_id):
        assert L.label_uid('seattle-wa', label_id) == 'seattle-wa:12327'

    def test_the_same_serial_in_two_cities_is_two_labels(self):
        assert L.label_uid('seattle-wa', 7) != L.label_uid('cdmx', 7)

    @pytest.mark.parametrize('city, label_id', [('', 1), ('a:b', 1), ('seattle-wa', 'x'), ('seattle-wa', None),
                                                ('seattle-wa', 1.5), ('seattle-wa', True), ('seattle-wa', 0),
                                                ('seattle-wa', -1), ('seattle-wa', '0')])
    def test_an_incomplete_or_ambiguous_reference_is_refused(self, city, label_id):
        with pytest.raises(ValueError):
            L.label_uid(city, label_id)


class TestVoteCounts:

    VOTES = [vote('Agree'), vote('Agree', 'AI'), vote('Disagree'), vote('Unsure', 'AI'), vote('Unsure')]

    def test_humans_only_by_default(self):
        assert L.vote_counts(self.VOTES) == VoteCounts(1, 1, 1)

    def test_with_ai(self):
        assert L.vote_counts(self.VOTES, include_ai=True) == VoteCounts(2, 1, 2)
        assert L.vote_counts(self.VOTES, include_ai=True).total == 5

    def test_include_ai_is_keyword_only(self):
        with pytest.raises(TypeError):
            L.vote_counts(self.VOTES, True)

    def test_no_votes(self):
        assert L.vote_counts([]) == VoteCounts(0, 0, 0)

    @pytest.mark.parametrize('bad', [{'validation': 'Agree', 'validator_type': 'Robot'},
                                     {'validation': 'agree', 'validator_type': 'Human'},
                                     {'validation': 'Agree'}, 'Agree', None])
    def test_a_vote_it_does_not_understand_raises(self, bad):
        with pytest.raises(ValueError):
            L.vote_counts([bad], include_ai=True)

    def test_an_unknown_type_raises_even_when_ai_is_excluded(self):
        """Excluding AI must not quietly count a new validator type as human."""
        with pytest.raises(ValueError):
            L.vote_counts([vote('Agree', 'Robot')])


class TestMajority:

    @pytest.mark.parametrize('counts, expected', [(VoteCounts(2, 1, 0), Verdict.AGREE),
                                                  (VoteCounts(1, 2, 5), Verdict.DISAGREE),
                                                  (VoteCounts(1, 0, 9), Verdict.AGREE)])
    def test_a_decisive_majority_ignores_the_tie_policy(self, counts, expected):
        for tie in L.TIE_POLICIES:
            assert L.majority(counts, tie=tie) is expected

    @pytest.mark.parametrize('tie', ['agree', 'disagree', 'unsure', 'none'])
    @pytest.mark.parametrize('counts', [VoteCounts(1, 1, 0), VoteCounts(0, 0, 3), VoteCounts(0, 0, 0)],
                             ids=['split', 'unsure-only', 'no-votes'])
    def test_every_tie_policy(self, counts, tie):
        assert L.majority(counts, tie=tie) is Verdict(tie)

    def test_the_tie_policy_is_required(self):
        with pytest.raises(TypeError):
            L.majority(VoteCounts(1, 0, 0))
        with pytest.raises(ValueError):
            L.majority(VoteCounts(1, 1, 0), tie='coin')

    def test_a_verdict_compares_as_its_string(self):
        assert L.majority(VoteCounts(1, 0, 0), tie='none') == 'agree'


class TestIsValidated:

    @pytest.mark.parametrize('counts, expected', [(VoteCounts(3, 1, 0), True),     # 0.75, at the boundary
                                                  (VoteCounts(2, 1, 0), False),    # 0.667
                                                  (VoteCounts(3, 0, 0), False),    # 3 votes < 4
                                                  (VoteCounts(3, 1, 9), True)])    # unsure abstains
    def test_thresholds_at_the_boundary(self, counts, expected):
        assert L.is_validated(counts, min_votes=4, min_agree_fraction=0.75) is expected

    @pytest.mark.parametrize('agree, disagree, fraction', [(7, 18, 0.28), (14, 11, 0.56), (1, 2, 1 / 3)])
    def test_a_share_exactly_at_the_threshold_passes(self, agree, disagree, fraction):
        """#229 review: `0.28 * 25` is 7.000000000000001, so the product form failed 7 of 25 at 0.28."""
        assert L.is_validated(VoteCounts(agree, disagree, 0), min_votes=1, min_agree_fraction=fraction)
        assert not L.is_validated(VoteCounts(agree - 1, disagree + 1, 0), min_votes=1, min_agree_fraction=fraction)

    def test_every_split_up_to_forty_votes_agrees_with_exact_arithmetic(self):
        from fractions import Fraction
        for decisive in range(1, 41):
            for agree in range(decisive + 1):
                share = agree / decisive                   # the double a caller would type for that share
                counts = VoteCounts(agree, decisive - agree, 0)
                assert L.is_validated(counts, min_votes=1, min_agree_fraction=share)
                for other in range(decisive + 1):
                    expected = Fraction(other, decisive) >= Fraction(agree, decisive)
                    assert L.is_validated(VoteCounts(other, decisive - other, 0), min_votes=1,
                                          min_agree_fraction=share) is expected

    def test_a_numpy_integer_is_an_integer(self):
        np = pytest.importorskip('numpy')
        assert L.is_validated(VoteCounts(2, 0, 0), min_votes=np.int64(2), min_agree_fraction=np.float64(1.0))
        assert L.label_uid('seattle-wa', np.int64(12327)) == 'seattle-wa:12327'

    def test_the_vote_floor_is_inclusive(self):
        assert L.is_validated(VoteCounts(2, 0, 0), min_votes=2, min_agree_fraction=1.0)
        assert not L.is_validated(VoteCounts(1, 0, 5), min_votes=2, min_agree_fraction=0.0)

    def test_a_label_nobody_judged_never_passes(self):
        assert not L.is_validated(VoteCounts(0, 0, 0), min_votes=1, min_agree_fraction=0.0)

    @pytest.mark.parametrize('kwargs', [{'min_votes': 0, 'min_agree_fraction': 0.5},
                                        {'min_votes': True, 'min_agree_fraction': 0.5},
                                        {'min_votes': 1.0, 'min_agree_fraction': 0.5},
                                        {'min_votes': 1, 'min_agree_fraction': 1.01},
                                        {'min_votes': 1, 'min_agree_fraction': float('nan')}])
    def test_a_bad_policy_is_refused(self, kwargs):
        with pytest.raises(ValueError):
            L.is_validated(VoteCounts(1, 0, 0), **kwargs)

    def test_both_policies_are_required(self):
        with pytest.raises(TypeError):
            L.is_validated(VoteCounts(1, 0, 0), min_votes=1)


class TestTheLiveShape:

    def test_the_fixture_parses_and_carries_no_user_id(self, features):
        assert len(features) == 14
        for feature in features:
            assert L.vote_counts(feature['validations'], include_ai=True).total == len(feature['validations'])
        with open(FIXTURE, encoding='utf-8') as f:
            assert '"user_id"' not in f.read()

    def test_both_validator_types_and_every_value_are_present(self, features):
        seen = {(v['validation'], v['validator_type']) for f in features for v in f['validations']}
        assert {t for _, t in seen} == set(L.VALIDATOR_TYPES)
        assert {v for v, _ in seen} == set(L.VALIDATION_VALUES)

    def test_the_served_counts_include_the_ai_validators_votes(self, features):
        for f in features:
            served = VoteCounts(f['agree_count'], f['disagree_count'], f['unsure_count'])
            assert L.vote_counts(f['validations'], include_ai=True) == served, f['label_id']
        assert any(L.vote_counts(f['validations']) != L.vote_counts(f['validations'], include_ai=True)
                   for f in features), 'the fixture must hold a label the AI votes change'

    def test_the_served_correct_is_the_tie_as_null_majority_with_ai(self, features):
        to_correct = {Verdict.AGREE: True, Verdict.DISAGREE: False, Verdict.NONE: None}
        for f in features:
            verdict = L.majority(L.vote_counts(f['validations'], include_ai=True), tie='none')
            assert to_correct[verdict] == f['correct'], f['label_id']
        assert {f['correct'] for f in features} == {True, False, None}
