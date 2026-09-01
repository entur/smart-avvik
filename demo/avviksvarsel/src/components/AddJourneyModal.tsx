import { useEffect, useMemo, useState } from 'react';
import { Modal } from '@entur/modal';
import { SearchableDropdown, type NormalizedDropdownItemType } from '@entur/dropdown';
import { TextField } from '@entur/form';
import { ChoiceChip, ChoiceChipGroup, FilterChip } from '@entur/chip';
import { BusIcon, TrainIcon } from '@entur/icons';
import { PrimaryButton, SecondaryButton } from '@entur/button';
import { SmallAlertBox } from '@entur/alert';
import { Label } from '@entur/typography';
import type { NewJourney, StopItem } from '../api';
import type { BrandId } from '../brand';
import { CONSENT_TEXT_VERSION } from '../personvern';
import { RegistreringsNotis } from './PersonvernNotis';
import { WEEKDAYS, WEEKDAY_SHORT } from '../format';

type Item = NormalizedDropdownItemType<string>;

const DEFAULT_DAYS = [0, 1, 2, 3, 4];

// The all-operator stop list runs to ~20k+ entries, so the dropdowns get their items from a
// ranked resolver (prefix matches first, then word-start, then substring; busiest stops first)
// capped at MAX_SHOWN — the full list is never rendered. Passing a function as `items` also
// makes SearchableDropdown skip its own label filter, so the resolver's ranking is what shows.
const MAX_SHOWN = 60;

function modeIcons(mode: string) {
  const icons = [];
  if (mode.includes('Tog')) icons.push(TrainIcon);
  if (mode.includes('Buss')) icons.push(BusIcon);
  return icons;
}

// Names that belong to more than one physical place ("Sentrum", "Kirken", ~1 300 of them). Those
// rows get their county appended, because a bare name repeated in a list is a coin flip: picking the
// wrong one registers a stop the traveller's trip never calls at, and the journey then reports
// "ingen aktive avvik akkurat nå" forever. Co-located refs are already merged server-side, so what
// is left here really is several different stops that happen to share a name.
function ambiguousNames(stops: StopItem[]): Set<string> {
  const places = new Map<string, Set<string>>();
  for (const s of stops) {
    const name = (s.stop_name ?? '').trim().toLowerCase();
    if (!name) continue;
    let seen = places.get(name);
    if (!seen) places.set(name, (seen = new Set()));
    seen.add(s.place_id ?? s.stop_place);
  }
  return new Set([...places].filter(([, refs]) => refs.size > 1).map(([name]) => name));
}

function stopLabel(s: StopItem, ambiguous: Set<string>): string {
  const name = s.stop_name ?? s.stop_place;
  const key = (s.stop_name ?? '').trim().toLowerCase();
  return s.region && ambiguous.has(key) ? `${name} (${s.region})` : name;
}

function rankItems(pool: StopItem[], query: string, ambiguous: Set<string>): Item[] {
  const q = query.trim().toLowerCase();
  const scored: { s: StopItem; rank: number }[] = [];
  for (const s of pool) {
    const name = (s.stop_name ?? s.stop_place).toLowerCase();
    if (q === '') {
      scored.push({ s, rank: 2 });
      continue;
    }
    const idx = name.indexOf(q);
    if (idx === -1) continue;
    // prefix match > start of a later word > anywhere in the name
    const rank = idx === 0 ? 0 : name[idx - 1] === ' ' || name[idx - 1] === '(' ? 1 : 2;
    scored.push({ s, rank });
  }
  scored.sort((a, b) => a.rank - b.rank || b.s.n_calls - a.s.n_calls);
  // One row per place, defensively: the server already collapses co-located refs, but an older
  // deploy (or an unreadable stop registry, which makes it fall back to one place per ref) would
  // otherwise put the duplicate rows straight back into the picker.
  const items: Item[] = [];
  const seen = new Set<string>();
  for (const { s } of scored) {
    const place = s.place_id ?? s.stop_place;
    if (seen.has(place)) continue;
    seen.add(place);
    items.push({
      value: s.stop_place,
      label: stopLabel(s, ambiguous),
      icons: modeIcons(s.mode),
      itemKey: s.stop_place,
    });
    if (items.length >= MAX_SHOWN) break;
  }
  return items;
}

export default function AddJourneyModal({
  open,
  onClose,
  stops,
  onCreate,
  brandId,
}: {
  open: boolean;
  onClose: () => void;
  stops: StopItem[];
  onCreate: (journey: NewJourney) => Promise<void>;
  /** Only so the privacy link keeps `?brand=none` across the navigation. */
  brandId: BrandId;
}) {
  const [mode, setMode] = useState('alle');
  const pool = useMemo(
    () => (mode === 'alle' ? stops : stops.filter((s) => s.mode.toLowerCase().includes(mode))),
    [stops, mode],
  );
  // Ambiguity is a property of the whole list, not of the current facet: a name stays labelled the
  // same way whether or not the Tog/Buss filter happens to hide its twin.
  const ambiguous = useMemo(() => ambiguousNames(stops), [stops]);
  // New resolver identity per pool change → the dropdowns re-resolve when the facet toggles.
  const items = useMemo(() => (text: string) => rankItems(pool, text, ambiguous), [pool, ambiguous]);
  const nameByRef = useMemo(
    () => new Map(stops.map((s) => [s.stop_place, s.stop_name ?? s.stop_place])),
    [stops],
  );
  const plainName = (item: Item) => nameByRef.get(item.value) ?? item.label;

  const [from, setFrom] = useState<Item | null>(null);
  const [to, setTo] = useState<Item | null>(null);
  const [begin, setBegin] = useState('07:00');
  const [end, setEnd] = useState('09:00');
  const [days, setDays] = useState<number[]>(DEFAULT_DAYS);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Reset the form each time the modal opens.
  useEffect(() => {
    if (open) {
      setMode('alle');
      setFrom(null);
      setTo(null);
      setBegin('07:00');
      setEnd('09:00');
      setDays(DEFAULT_DAYS);
      setError(null);
      setSaving(false);
    }
  }, [open]);

  function toggleDay(d: number) {
    setDays((prev) => (prev.includes(d) ? prev.filter((x) => x !== d) : [...prev, d].sort((a, b) => a - b)));
  }

  function validate(): string | null {
    if (!from) return 'Velg et fra-stopp.';
    if (!to) return 'Velg et til-stopp.';
    if (from.value === to.value) return 'Fra- og til-stopp må være ulike.';
    if (!begin || !end) return 'Velg tidsrom.';
    if (begin >= end) return 'Tidsrommet må starte før det slutter.';
    if (days.length === 0) return 'Velg minst én ukedag.';
    return null;
  }

  async function submit() {
    const problem = validate();
    if (problem) {
      setError(problem);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      // The plain registry name, not the picker label: the label may carry a county suffix added
      // only to tell two same-named stops apart, and this name is what the journey card and the
      // written push text show.
      await onCreate({
        from_stop: from!.value,
        from_name: plainName(from!),
        to_stop: to!.value,
        to_name: plainName(to!),
        begin,
        end,
        days,
        // Which version of the registration-visible texts this user actually saw. Stored on the
        // journey record so the consent is provable after the wording changes.
        consent_text_version: CONSENT_TEXT_VERSION,
      });
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal open={open} onDismiss={onClose} size="medium" title="Legg til reise">
      <div className="av-form">
        <ChoiceChipGroup
          name="stop-mode"
          label="Transportmiddel"
          value={mode}
          onChange={(e) => setMode(e.target.value)}
        >
          <ChoiceChip value="alle">Alle</ChoiceChip>
          <ChoiceChip value="tog">Tog</ChoiceChip>
          <ChoiceChip value="buss">Buss</ChoiceChip>
        </ChoiceChipGroup>
        <SearchableDropdown
          label="Fra"
          items={items}
          debounceTimeout={0}
          selectedItem={from}
          onChange={(item) => setFrom(item)}
          placeholder="Søk etter stopp"
          clearable
        />
        <SearchableDropdown
          label="Til"
          items={items}
          debounceTimeout={0}
          selectedItem={to}
          onChange={(item) => setTo(item)}
          placeholder="Søk etter stopp"
          clearable
        />

        <div className="av-form-times">
          <TextField
            label="Fra klokkeslett"
            type="time"
            value={begin}
            onChange={(e) => setBegin(e.target.value)}
          />
          <TextField
            label="Til klokkeslett"
            type="time"
            value={end}
            onChange={(e) => setEnd(e.target.value)}
          />
        </div>

        <fieldset className="av-form-days">
          <Label as="legend">Ukedager</Label>
          <div className="av-chips" role="group" aria-label="Ukedager">
            {WEEKDAYS.map((d) => (
              <FilterChip
                key={d}
                name="weekday"
                type="checkbox"
                value={String(d)}
                checked={days.includes(d)}
                onChange={() => toggleDay(d)}
              >
                {WEEKDAY_SHORT[d]}
              </FilterChip>
            ))}
          </div>
        </fieldset>

        {error && (
          <SmallAlertBox variant="negative" width="fluid">
            {error}
          </SmallAlertBox>
        )}

        {/* T3, directly above the buttons: what this form collects, why, and how to undo it.
            Registration stays confirmation-style — this is the information that makes tapping
            «Lagre reise» informed, not a consent checkbox. */}
        <RegistreringsNotis brandId={brandId} />

        <div className="av-form-actions">
          <SecondaryButton onClick={onClose} disabled={saving}>
            Avbryt
          </SecondaryButton>
          <PrimaryButton onClick={submit} loading={saving}>
            Lagre reise
          </PrimaryButton>
        </div>
      </div>
    </Modal>
  );
}
