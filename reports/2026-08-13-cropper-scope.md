# What the cropper is for, and how far this study should go

**2026-08-13** · design note, not a measurement study · cropper work package

This one is a scope decision rather than an investigation. It exists because two days of
corpus assembly and one afternoon of actually annotating produced a study that kept widening
while the thing it was meant to improve — the cropper — stayed where it was. It records what
the cropper is actually for, what evidence it needs, and what belongs in a different repo
entirely.

Every number below is transcribed from a committed artifact or from a report named at the
point of use. Nothing here is new measurement.

---

## 1. What prompted it

Annotating the first tiles surfaced three questions that the protocol could not answer, and
they turned out to be the same question:

* An `Obstacle` tagged `stairs` has no centre and no extent that survive a change of viewing
  angle. Nor do `cracks`, `grass`, or `uneven/slanted` — which are 42 of the 46 surviving
  SurfaceProblem labels in the corpus.
* For a fire hydrant, does the box go round the hydrant, or round the part of it that blocks
  the footway? CV convention says the object; Project Sidewalk cares about the impedance.
  Both are defensible and they differ by a lot.
* Which label types are even worth measuring, given that five of the original eight had
  already been excluded for having no locatable referent?

The common root: **nobody had written down what the crop is for**, so every one of these was
being answered from taste.

## 2. Three things that settle it

**(a) The crop is context, not the object.** From the
[consumer requirements survey](2026-08-09-cropper-consumer-requirements.md): the four
consumers converge on the object occupying **10–15% of the crop side** — the validator is
**12.6%** by construction, a 1.5 m ramp inside an 11.9 m footprint, giving a crop ratio
**R = 6.7–10×**. So the box an annotator drags is a *scale reference*, not the thing the crop
frames. The crop shows the situation; the object is a small thing in the middle of it.

The consequence is the useful part: if crop size is mostly a fixed metric footprint, it is
**nearly type-independent by construction**. A pole, a hydrant and a curb ramp all want about
the same window, because what a reviewer needs to see is the pathway. Object size only starts
driving crop size when the object is *larger* than that floor — crosswalks and missing
sidewalk — and those are already outside the measurable set for having no located referent.

**(b) The binding consumer is RampNet, and RampNet is curb ramps.** The same survey sets
mis-centering from "RampNet's supervision, not containment": target **≤ 0.5°** in the pano
frame, against consumer ceilings of ~1% of crop side for RampNet, ~6% for the validator and
~10% for the tagger. Every other consumer has 2–10× more slack. So a study that measures
curb ramps is not a compromise on generality — it is aimed at the consumer that sets the
number everything else is compared against.

**(c) The centering defect is real and it is a curb-ramp defect.** The status quo is a
**1–3° tilt residual**, which the survey notes "violates the RampNet target by 2–6× and
consumes most of the validator's far-field budget". This is the part that is actually broken,
and it sits squarely inside the narrowed scope rather than being a casualty of it.

## 3. The decision

**Build an excellent curb-ramp cropper and apply it to all types, saying so.**

Concretely:

* Gold is collected on **curb ramps**, drawn stratified by **distance** rather than by
  depression band — distance is what the size rule is a function of, and the far tail is
  where the current formula is worst.
* The size rule is fit there and shipped for every label type, with the caveat stated:
  it is a context-floor rule, it was fit on curb ramps, and it is expected to be wrong in
  extent for referents larger than the floor (crosswalks, missing sidewalk).
* Centering is fit on the same corpus, which is exactly the population the ≤ 0.5° target
  was written for.

This is already an improvement over what ships today. `predict_crop_size` is
`8725.6 · d^-1.192` clamped to [50, 1500] — distance in, size out, no object term at all —
and it was fit on the 2013 Tohme curb-ramp boxes (see §6). Replacing a curb-ramp fit from
2013 with a curb-ramp fit from current imagery, on a corpus drawn for the purpose, is a
strictly better version of the same rule.

**What this deletes.** The eleven-pair `(label_type, tag)` exclusion rule, the four-type cell
balance, the amorphous-referent argument, and the question of whether `Obstacle` survives —
none of it applies to a single well-defined type. That machinery stays in `rawlabels.py`
because the corpus draw still uses it, but it stops being load-bearing for the decision.

**Open:** whether `NoCurbRamp` comes along. Same corner, same distance regime, same crop
geometry, and it roughly doubles usable n. Against it: the referent is an absence, so the box
is a judgement in a way a ramp's is not. Leaning toward *in* for sizing and *out* for
centering.

## 4. The box question, and why it is not the annotator's to settle

The hydrant question has a clean answer once the two marks are recognised as doing different
jobs:

| mark | carries | convention |
|---|---|---|
| **point** | Project Sidewalk's semantics — how the thing impedes a path | at ground contact, per the type rubric |
| **box** | the object's extent, as a scale reference and as future detector supervision | ordinary CV convention: the **whole object** |

Keeping impedance *out* of the box is the substance, not an oversight. "How much of this
object blocks the path" is a modelling decision, and gold that bakes one answer into its
geometry can never be used to compare another. Recording object extent plus ground contact
leaves width-only, footprint-derived and distance-only rules all still testable against the
same annotations.

It is also the more reproducible mark, which matters because the agreement gate is what
licenses the gold: a silhouette is visible, while "where it becomes a barrier" is a
judgement. This is now `annotation_tiles.BOX_RULE`, served to the page from code.

**A rejected alternative worth recording.** The cropper's output is square —
`CropBox(left, top, size, shifted)`, one scalar — so the box's second dimension is currently
unread. That made a *ground-contact span* tempting: one drag, unambiguous, exactly what a
square crop needs. It was rejected because it produces a number only the current cropper can
consume, and destroys the annotation's value for any future detector. The right resolution is
that object height re-enters through **containment** — a crop a detector trains on has to
contain the object — rather than through the size rule's proportional term.

## 5. The size rule this implies

Not `R × extent`. At R ≈ 8 a 0.2 m pole would get a 1.6 m crop, which shows nothing about
whether it blocks anything. Three terms, to be fit and compared:

```
crop_side = max(context_floor(distance),          # enough pathway to judge impedance
                R × object_extent,                 # binds only for large referents
                containment(object, distance))     # a detector's crop must contain the object
```

The current formula has none of these; it is distance-only. That it works tolerably at all is
itself evidence for (a): a distance-only rule is a context floor with the object term missing,
and the object term rarely binds.

## 6. What the Tohme data can and cannot do

Found on lab storage 2026-08-13:
`/m-makeabilitylab/makeabilitylab/sidewalk2/Dataset/2013-11-18_GroundTruthDir.zip` — **2,862**
human-drawn boxes over **741** panos (1,086 files, 345 deliberately empty), in full-pano
coordinates, single class, with all 1,086 panos' JPEGs and 2013-era `.depth.txt` beside them
at `sidewalk2/Panoramas/tohme` (31 GB). The deleted `crop_from_tohme.py` (commit `90685183`,
Apr 2023) is what names the paths.

Two limits, and the second is decisive:

1. **`predict_crop_size` was fit on it.** That script logs predicted-vs-actual crop size per
   box against exactly these annotations. Validating today's formula against Tohme is scoring
   on its own training set.
2. **It cannot serve a stored-click-vs-gold study.** Joining the 1,086 pano ids against the
   `sidewalk_dc` database (makelab1:5434) yields **1 overlapping pano, 10 labels** — there
   are no paired Project Sidewalk clicks. That schema's `old_label_metadata` is 271,187 rows
   of `sv_image_x/y`: points, no boxes anywhere in it.

So it is a sizing asset with an independence problem, not a centering one. Its honest use is
as a held-out sanity check on crop *containment* for curb ramps, stated as non-independent.

## 7. What does not belong in this repo

The modern version of this problem is not a formula. It is:

1. depth — from `streetlevel`, which this repo already downloads, or from a modern monocular
   estimator;
2. semantic segmentation to find the sidewalk surface;
3. the intersection of the object with that surface — the impedance region, computed rather
   than annotated;
4. object detection/segmentation on top, for auto-tagging and severity.

That pipeline **largely obsoletes the heuristic cropper**: given a detection you crop from
the detection, not from a size formula. Which is an argument for spending *less* here and
shipping sooner, not for expanding this study to anticipate it.

It belongs in `sidewalk-auto-labeler` or a RampNet 3.0, because that is where models, weights
and GPUs live. This repo downloads panoramas and cuts crops.

**What survives the transition — less than this section first claimed.** It originally said that
crops for *human* consumption survive: that the validator UI and share images need a context
window whether or not a segmenter exists, so the work has a permanent piece in it. **That is
wrong. It was checked on 2026-08-14 rather than assumed, and the check refutes it.**

The crops the human UI shows are not this tool's. When a user places a label the browser captures
the Explore canvas and uploads it — `Label.js#uploadCrop` → `POST /saveImage` →
`ImageController.writeImageFile`, which resizes to 1440×960 and writes
`<city-id>/<LabelType>/crop_<labelId>.png`, the same path `PanoDataService.getCropDirectory`
serves from. `ShareController` composites its social preview out of that stored crop, falling
back to a Street View still or a branded placeholder — never to a cut from this tool. The
human-facing crop already exists, produced at label time by a mechanism with no size formula
anywhere in it.

The four consumers in the [requirements survey](2026-08-09-cropper-consumer-requirements.md) —
RampNet 2.0, `sidewalk-validator-ai`, `sidewalk-tagger-ai`, `sidewalk-ai-api` — are **all ML
pipelines**. That is the entire current consumer set for `CropRunner`, and it is exactly the set
a detection-first pipeline replaces.

What the canvas capture *cannot* do is what still justifies this tool: it exists only for labels
placed after that feature shipped, and its geometry is whatever the annotator's viewport happened
to show — variable POV and zoom, 3:2, uncontrolled. Training over the full historical corpus
needs controlled, reproducible windows, and only this tool produces those.

So the honest version: **nothing survives on the human side, and the ML side's lifetime is
bounded by the pipeline in this section.** That shortens the bridge. It does not change its
direction — ship a context floor, spend less here, and spend it sooner.

## 8. Wrong turns

* **Claiming a permanent human-facing consumer without checking who writes the file.** §7 asserted
  that the validator UI and share images need this cropper's context window, and made that the
  reason the work has a permanent piece. Neither is served by this tool — the browser writes
  `crop_<labelId>.png` from the Explore canvas at label time. The requirements survey never said
  otherwise: "the validator" in it is `sidewalk-validator-ai`, a model, and it was read as the
  human validate page. Every consumer in that survey is ML. The check that settles it is one
  search for who writes `crop_<labelId>.png`, and it took two minutes once asked.
* **Treating the drawn corpus as fixed because it was "already registered".** Filtering
  rather than redrawing was recommended on that basis. It is not a reason — the corpus after
  five type-exclusions was a leftover, not a design, and the question was always which dataset
  best supports building a good cropper. Registration vocabulary was dropped from §7 of the
  pre-registration earlier the same day and then kept being used as an *argument*, which is
  the worse half of the same mistake.
* **Sizing the referent rule against the corpus it was derived from.** The tag exclusions were
  proposed from a principle and had to be re-decided pair by pair against the actual tag
  vocabulary, where `height difference` turned out to be a region under SurfaceProblem and an
  object under Obstacle.
* **Assuming a wider annotation task was a wider study.** Five of eight label types were
  annotated for two days before anyone asked whether their crops were sized by the same rule.

## 9. Sources

| Claim | Source |
|---|---|
| Object 10–15% of crop side; R = 6.7–10; validator 12.6% / 11.9 m / 1.5 m | [2026-08-09-cropper-consumer-requirements.md](2026-08-09-cropper-consumer-requirements.md) §(ii) |
| Placement target ≤ 0.5°; status quo 1–3°, 2–6× over; consumer ceilings | same, §(i) |
| `8725.6 · d^-1.192`, clamp [50, 1500] | `CropRunner.predict_crop_size` |
| Corpus composition, measurable counts, per-type survivors | [data/2026-08-12-crop-corpus-gsv.csv.gz](data/2026-08-12-crop-corpus-gsv.csv.gz) under `rawlabels.has_located_referent` + `in_study_frame` |
| Tohme box counts, pano coverage, `sidewalk_dc` overlap | measured 2026-08-13 on makelab1/makelab2; paths in §6 |
| Production crops are browser canvas captures, not this tool's output | SidewalkWebpage `public/js/explore/src/label/Label.js` (`#uploadCrop`), `app/controllers/ImageController.scala` (`saveImage`, `writeImageFile`, 1440×960), `app/controllers/ShareController.scala` (crop → GSV still → placeholder), `conf/routes`; read 2026-08-14 |
