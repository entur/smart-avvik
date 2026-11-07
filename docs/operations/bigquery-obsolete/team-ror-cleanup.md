# Cleaning up your copy of kix-avvik's dev dataset

**For:** team-ror (smart-avvik). **Written:** 2026-11-07.

On 2026-11-06 you copied every object in `ent-kixavvik-dev.disruptions` into `ent-smartavvik-dev`:
556 objects, most of them experiments. We have since moved every object the service does not need
out of `disruptions` and into `disruptions_obsolete` in the same project. An object stays in
`disruptions` when deployed code, a scheduled query, the prd copy, a retrain procedure or `/demo`
uses it. Everything else moved.

This file tells you how to delete the same objects from your copy, so it holds what the service
uses and nothing else.

## What to delete

Every row in [`manifest-dev.csv`](manifest-dev.csv) whose `verdict` is `move`. Keep the `keep`
rows. The `reason` column says why each object moved: mostly `unreferenced` (nothing names it) or
`eval-only` (only an evaluation notebook reads it). A row we decided by hand keeps the reason it
was flagged for, such as `external-read`.

Nothing to do in prd or tst. Your prd copy holds only `varsel_decisions` and
`varsel_decision_outcomes`, and both stay. tst has no objects.

## Before you start

1. Wait until we tell you the dev move is done. We send this file when it has passed its checks.
2. Check that nothing of yours names a moved object. A copy job with a fixed list fails once its
   source has moved, so take those names out of the job too.

   ```sh
   MANIFEST=manifest-dev.csv   # docs/operations/bigquery-obsolete/ in entur/kix-avvik
   awk -F, 'NR > 1 && $5 == "move" {print $1}' "$MANIFEST" > moved.txt
   grep -rnwF -f moved.txt path/to/your/repo
   ```

   Any hit is an object you use. Leave it in your copy, and tell us if your code reads it from our
   `disruptions`.

## Delete

A `move` row is not proof that we moved the object. Our mover skips an object that changed after
the manifest was written, and we may have restored one since. So the loop deletes your copy only
when the object is gone from our `disruptions`, and keeps it otherwise.

Set `DEST` to the dataset you copied into. Run once as it stands, which only prints the commands,
and check the count against the manifest's `move` rows. Then set `RUN=` and run again to delete.

```sh
MANIFEST=manifest-dev.csv
OURS=ent-kixavvik-dev:disruptions
DEST=ent-smartavvik-dev:YOUR_DATASET
RUN=echo                    # dry run; set RUN= to delete

# A failed read below means "gone", so first prove you can read our dataset at all.
bq show --format=none "$OURS.training_calls" || { echo "cannot read $OURS; fix that first" >&2; exit 1; }

awk -F, 'NR > 1 && $5 == "move" {print $2, $1}' "$MANIFEST" |
while read -r type name; do
  case $type in
    MODEL) ours=(--model) rm=(-m) ;;
    TABLE) ours=() rm=(-t) ;;
    *)     echo "unexpected type $type for $name" >&2; continue ;;
  esac
  if bq show "${ours[@]}" --format=none "$OURS.$name" >/dev/null 2>&1; then
    echo "still in our dataset, keeping yours: $name"
    continue
  fi
  $RUN bq rm -f "${rm[@]}" "$DEST.$name"
done
```

The first five columns (`name`, `type`, `size_gb`, `modified`, `verdict`) never contain a comma,
so splitting on commas is safe for them. A `Not found` error from `bq rm` is expected for an object
that we created after your copy: you never had it.

## Getting an object back

We delete nothing for good. `ent-kixavvik-dev.disruptions_obsolete` keeps every moved object under
its own name, with no expiry. Ask whoever runs kix-avvik to restore it. The restore puts it back in
`disruptions`, where your existing read access covers it, and you copy it from there as before:

```sh
uv run python scripts/bq_obsolete_move.py --project ent-kixavvik-dev --restore --only NAME --apply
```
