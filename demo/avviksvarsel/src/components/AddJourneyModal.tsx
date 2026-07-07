import { useEffect, useMemo, useState } from 'react';
import { Modal } from '@entur/modal';
import { SearchableDropdown, type NormalizedDropdownItemType } from '@entur/dropdown';
import { TextField } from '@entur/form';
import { FilterChip } from '@entur/chip';
import { PrimaryButton, SecondaryButton } from '@entur/button';
import { SmallAlertBox } from '@entur/alert';
import { Label } from '@entur/typography';
import type { NewJourney, StopItem } from '../api';
import { WEEKDAYS, WEEKDAY_SHORT } from '../format';

type Item = NormalizedDropdownItemType<string>;

const DEFAULT_DAYS = [0, 1, 2, 3, 4];

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
  const items = useMemo<Item[]>(
    () =>
      stops
        .map((s) => ({ value: s.stop_place, label: s.stop_name ?? s.stop_place }))
        .sort((a, b) => a.label.localeCompare(b.label, 'nb')),
    [stops],
  );

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
        <SearchableDropdown
          label="Fra"
          items={items}
          selectedItem={from}
          onChange={(item) => setFrom(item)}
          placeholder="Søk etter stopp"
          clearable
        />
        <SearchableDropdown
          label="Til"
          items={items}
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
