"""Kinematics: roundtrips, Jacobian, tensions, pre-compensation.

These run before anything is ever sent to hardware, on purpose: a kinematics
error only becomes visible on the board once wrong strokes are already drawn.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from wbbot.core.kinematics import (BeltLengths, GRAVITY_M_S2, KinematicsError,
                                   NaiveKinematics, TangentKinematics,
                                   UnreachableError, make_kinematics)

#: Board interior, safely away from the anchors.
BOARD_X = st.floats(min_value=5.0, max_value=1495.0, allow_nan=False)
BOARD_Y = st.floats(min_value=5.0, max_value=995.0, allow_nan=False)

TOLERANCE_MM = 0.01


# ----------------------------------------------------------------------
# roundtrips
# ----------------------------------------------------------------------

@pytest.mark.parametrize("model", ["naive", "tangent"])
@settings(max_examples=250, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(x=BOARD_X, y=BOARD_Y)
def test_roundtrip_property(cfg, model, x, y):
    """forward(inverse(P)) == P to better than 0.01 mm, everywhere."""
    kin = make_kinematics(cfg, model)
    p = np.array([x, y])
    back = kin.forward(kin.inverse(p))
    assert np.linalg.norm(back - p) < TOLERANCE_MM


@pytest.mark.parametrize("model", ["naive", "tangent"])
def test_roundtrip_grid(cfg, model):
    """Dense sweep of the whole board, vectorised."""
    kin = make_kinematics(cfg, model)
    xs = np.linspace(2.0, 1498.0, 61)
    ys = np.linspace(2.0, 998.0, 41)
    gx, gy = np.meshgrid(xs, ys)
    pts = np.stack([gx.ravel(), gy.ravel()], axis=1)
    back = kin.forward_many(kin.inverse_many(pts))
    err = np.linalg.norm(back - pts, axis=1)
    assert err.max() < TOLERANCE_MM, f"worst roundtrip error {err.max():.6f} mm"


def test_roundtrip_corners(cfg):
    """Corners are where a sloppy branch choice shows up first."""
    for model in ("naive", "tangent"):
        kin = make_kinematics(cfg, model)
        for p in [(0, 0), (1500, 0), (0, 1000), (1500, 1000), (750, 500)]:
            back = kin.forward(kin.inverse(p))
            assert np.allclose(back, p, atol=TOLERANCE_MM), (model, p, back)


def test_forward_picks_the_solution_below_the_anchor_line(cfg, naive):
    """The gondola hangs — the mirrored solution above the belts is wrong."""
    anchor_y = cfg.geometry.anchor_left[1]
    for p in [(100, 900), (750, 950), (1400, 900)]:
        back = naive.forward(naive.inverse(p))
        assert back[1] < anchor_y
        assert np.allclose(back, p, atol=TOLERANCE_MM)


# ----------------------------------------------------------------------
# tangent model specifics
# ----------------------------------------------------------------------

def test_tangent_degenerates_to_naive_with_zero_radius(cfg):
    a, b = cfg.geometry.anchor_left, cfg.geometry.anchor_right
    naive = NaiveKinematics(a, b, 0.0)
    tangent = TangentKinematics(a, b, 0.0)
    pts = np.array([[100, 100], [750, 500], [1400, 900]], dtype=float)
    assert np.allclose(naive.inverse_many(pts), tangent.inverse_many(pts), atol=1e-9)


def test_tangent_differs_from_naive_by_a_meaningful_amount(cfg, naive, tangent):
    """The wrap term is the part that matters, and it is not small.

    The pure sqrt(D^2-r^2) correction is worth 0.08 mm at D=1000; the pulley
    wrap sweeps well over a centimetre of belt across the board.  If this
    assertion ever fails, the wrap term has been dropped somewhere.
    """
    xs = np.linspace(0, 1500, 25)
    ys = np.linspace(0, 1000, 17)
    gx, gy = np.meshgrid(xs, ys)
    pts = np.stack([gx.ravel(), gy.ravel()], axis=1)
    delta = tangent.inverse_many(pts) - naive.inverse_many(pts)
    span = float(delta.max() - delta.min())
    assert span > 10.0, f"wrap term only spans {span:.2f} mm — is it missing?"


def test_tangent_belt_length_grows_with_distance(cfg, tangent):
    """Monotonicity along a ray away from the left anchor."""
    a = np.array(cfg.geometry.anchor_left)
    direction = np.array([0.6, -0.8])
    lengths = [tangent.inverse(a + direction * d).left for d in range(200, 1400, 25)]
    assert all(b > a_ for a_, b in zip(lengths, lengths[1:]))


def test_tangent_belt_jacobian_matches_finite_differences(cfg, tangent):
    h = 1e-4
    for p in [(200, 200), (750, 500), (1300, 850)]:
        analytic = tangent.belt_jacobian(np.array([p], dtype=float))[0]
        numeric = np.zeros((2, 2))
        for axis in (0, 1):
            step = np.zeros(2)
            step[axis] = h
            plus = tangent.inverse_many((np.array(p) + step).reshape(1, 2))[0]
            minus = tangent.inverse_many((np.array(p) - step).reshape(1, 2))[0]
            numeric[:, axis] = (plus - minus) / (2 * h)
        assert np.allclose(analytic, numeric, atol=1e-6), (p, analytic, numeric)


def test_point_inside_pulley_is_rejected(cfg, tangent):
    with pytest.raises(UnreachableError):
        tangent.inverse(cfg.geometry.anchor_left)


def test_impossible_belt_lengths_are_rejected(cfg, naive):
    with pytest.raises(UnreachableError):
        naive.forward(BeltLengths(1.0, 1.0))       # circles do not meet
    with pytest.raises(UnreachableError):
        naive.forward(BeltLengths(-5.0, 100.0))


# ----------------------------------------------------------------------
# conditioning and statics
# ----------------------------------------------------------------------

def test_error_amplification_is_finite_and_modest_over_the_board(cfg, naive):
    xs = np.linspace(0, 1500, 31)
    ys = np.linspace(0, 1000, 21)
    worst = 0.0
    for x in xs:
        for y in ys:
            amp = naive.error_amplification((x, y))
            assert math.isfinite(amp)
            worst = max(worst, amp)
    assert worst < 4.0, f"error amplification reaches {worst:.2f} on the board"


def test_error_amplification_explodes_near_the_anchor_line(cfg, naive):
    """Just below the anchor line the belts are nearly collinear."""
    anchor_y = cfg.geometry.anchor_left[1]
    close = naive.error_amplification((750, anchor_y - 2.0))
    far = naive.error_amplification((750, 500))
    assert close > 10 * far


def test_error_amplification_explodes_far_below(cfg, naive):
    """Far below the anchors both belts become parallel.

    At the board centre the belts happen to be perpendicular, which is the
    best case (amplification exactly 1.0). Going down, the amplification grows
    without bound — check both the monotonic trend and the magnitude.
    """
    depths = [500, -2_000, -20_000, -200_000]
    amps = [naive.error_amplification((750, y)) for y in depths]
    assert all(b > a for a, b in zip(amps, amps[1:])), amps
    assert amps[0] == pytest.approx(1.0, abs=0.05)
    assert amps[-1] > 100.0, amps


def test_tensions_are_positive_over_the_board_and_carry_the_weight(cfg, naive):
    weight = cfg.gondola.mass_kg * GRAVITY_M_S2
    for x in np.linspace(0, 1500, 16):
        for y in np.linspace(0, 1000, 11):
            t = naive.tensions((x, y))
            assert t.left > 0 and t.right > 0, (x, y, t)
            u1 = (naive.anchor_left - np.array([x, y]))
            u1 = u1 / np.linalg.norm(u1)
            u2 = (naive.anchor_right - np.array([x, y]))
            u2 = u2 / np.linalg.norm(u2)
            total = t.left * u1 + t.right * u2
            assert abs(total[0]) < 1e-6
            assert abs(total[1] - weight) < 1e-6


def test_tension_goes_negative_outside_the_anchor_span(cfg, naive):
    """A belt cannot push: outside the anchors one tension must go negative."""
    left_of_left = naive.tensions((cfg.geometry.anchor_left[0] - 200, 500))
    right_of_right = naive.tensions((cfg.geometry.anchor_right[0] + 200, 500))
    assert left_of_left.minimum < 0
    assert right_of_right.minimum < 0


def test_symmetry_of_tensions_at_the_centre(cfg, naive):
    mid_x = 0.5 * (cfg.geometry.anchor_left[0] + cfg.geometry.anchor_right[0])
    t = naive.tensions((mid_x, 500))
    assert t.left == pytest.approx(t.right, rel=1e-9)


# ----------------------------------------------------------------------
# pre-compensation
# ----------------------------------------------------------------------

def test_precompensation_is_identity_for_the_naive_model(naive):
    p = (700.0, 400.0)
    assert np.allclose(naive.precompensate(p), p)


def test_precompensation_is_zero_at_the_reference(tangent):
    ref = (750.0, 500.0)
    assert np.allclose(tangent.precompensate(ref, ref), ref, atol=1e-9)


def test_precompensation_reproduces_the_tangent_belt_lengths(cfg, naive, tangent):
    """The whole point: FluidNC's naive maths on P' must give tangent(P)."""
    ref = np.array([750.0, 500.0])
    ref_naive = naive.inverse_many(ref.reshape(1, 2))[0]
    ref_tan = tangent.inverse_many(ref.reshape(1, 2))[0]
    for p in [(100, 100), (750, 900), (1400, 200), (50, 950)]:
        p = np.array(p, dtype=float)
        p_cmd = tangent.precompensate(p, ref)
        produced = naive.inverse_many(p_cmd.reshape(1, 2))[0] - ref_naive
        wanted = tangent.inverse_many(p.reshape(1, 2))[0] - ref_tan
        assert np.allclose(produced, wanted, atol=1e-6), (p, produced, wanted)


def test_precompensation_displacement_is_significant_but_bounded(tangent):
    ref = (750.0, 500.0)
    xs = np.linspace(0, 1500, 21)
    ys = np.linspace(0, 1000, 15)
    gx, gy = np.meshgrid(xs, ys)
    pts = np.stack([gx.ravel(), gy.ravel()], axis=1)
    moved = tangent.precompensate_many(pts, ref)
    d = np.linalg.norm(moved - pts, axis=1)
    assert 1.0 < d.max() < 100.0, f"precompensation moves points by {d.max():.2f} mm"


def test_precompensate_many_matches_the_scalar_version(tangent):
    ref = (750.0, 500.0)
    pts = np.array([[100, 100], [750, 500], [1400, 900]], dtype=float)
    batch = tangent.precompensate_many(pts, ref)
    for p, b in zip(pts, batch):
        assert np.allclose(tangent.precompensate(p, ref), b, atol=1e-9)


# ----------------------------------------------------------------------
# construction / validation
# ----------------------------------------------------------------------

def test_anchor_order_is_validated():
    with pytest.raises(KinematicsError):
        NaiveKinematics((1600, 1350), (-100, 1350))


def test_non_finite_input_is_rejected(naive):
    with pytest.raises(KinematicsError):
        naive.inverse((float("nan"), 100.0))


def test_make_kinematics_rejects_unknown_model(cfg):
    with pytest.raises(KinematicsError):
        make_kinematics(cfg, "spline")   # type: ignore[arg-type]


def test_shape_is_preserved_by_vectorised_calls(tangent):
    pts = np.random.default_rng(0).uniform([10, 10], [1490, 990], size=(4, 5, 2))
    lengths = tangent.inverse_many(pts)
    assert lengths.shape == pts.shape
    back = tangent.forward_many(lengths)
    assert back.shape == pts.shape
    assert np.abs(back - pts).max() < TOLERANCE_MM
