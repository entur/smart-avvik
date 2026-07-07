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

function rankItems(pool: StopItem[], query: string): Item[] {
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
  return scored.slice(0, MAX_SHOWN).map(({ s }) => ({
    value: s.stop_place,
    label: s.stop_name ?? s.stop_place,
    icons: modeIcons(s.mode),
    itemKey: s.stop_place,
  }));
}

export default function AddJourneyModal({
  open,
  onClose,
  stops,
  onCreate,
}: {
  open: boolean;
  onClose: () => void;
  stops: StopItem[];
  onCreate: (journey: NewJourney) => Promise<void>;
}) {
  const [mode, setMode] = useState('alle');
  const pool = useMemo(
    () => (mode === 'alle' ? stops : stops.filter((s) => s.mode.toLowerCase().includes(mode))),
    [stops, mode],
  );
  // New resolver identity per pool change → the dropdowns re-resolve when the facet toggles.
  const items = useMemo(() => (text: string) => rankItems(pool, text), [pool]);

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
      await onCreate({
        from_stop: from!.value,
        from_name: from!.label,
        to_stop: to!.value,
        to_name: to!.label,
        begin,
        end,
        days,
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
