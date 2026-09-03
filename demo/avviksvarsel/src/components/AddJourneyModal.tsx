import { useEffect, useMemo, useRef, useState, type FocusEvent } from 'react';
import { Modal } from '@entur/modal';
import { SearchableDropdown, type NormalizedDropdownItemType } from '@entur/dropdown';
import { TextField } from '@entur/form';
import { ChoiceChip, ChoiceChipGroup, FilterChip } from '@entur/chip';
import { BusIcon, TrainIcon, VerticalSwitchIcon } from '@entur/icons';
import { IconButton, PrimaryButton, SecondaryButton } from '@entur/button';
import { SmallAlertBox } from '@entur/alert';
import { Label, SmallText } from '@entur/typography';
import type { NewJourney, StopItem } from '../api';
import type { BrandId } from '../brand';
import { CONSENT_TEXT_VERSION } from '../personvern';
import { RegistreringsNotis } from './PersonvernNotis';
import { WEEKDAYS, WEEKDAY_SHORT } from '../format';
import { ambiguousNames, rankStops, stopLabel } from './stopResolver';

type Item = NormalizedDropdownItemType<string>;

const DEFAULT_DAYS = [0, 1, 2, 3, 4];

// A registered window longer than this earns a hint, not a refusal (design review 2026-09-01,
// finding 38): 07:00–22:00 makes the window useless as a noise filter and multiplies the pushes,
// but a shift worker's day is legitimately long, so the form says so and lets them decide.
const LONG_WINDOW_MIN = 6 * 60;

function modeIcons(mode: string) {
  const icons = [];
  if (mode.includes('Tog')) icons.push(TrainIcon);
  if (mode.includes('Buss')) icons.push(BusIcon);
  return icons;
}

// Validation names the FIELD, not just the fault (design review 2026-09-01, findings 6 and 19).
// One cascade returning one string rendered as one alert box at the bottom of the modal, whichever
// field was missing, and a screen-reader user who pressed «Lagre reise» heard nothing at all: no
// aria-invalid, no aria-describedby, no live region. The message now renders under the field it
// is about (Linje's `variant`/`feedback` wire the aria attributes), focus moves there, and the
// wording is the traveller's language rather than the form's.
type Field = 'from' | 'to' | 'time' | 'days';

interface Problem {
  field: Field;
  message: string;
}

function minutesBetween(begin: string, end: string): number | null {
  const parse = (hhmm: string) => {
    const [h, m] = hhmm.split(':').map(Number);
    return Number.isFinite(h) && Number.isFinite(m) ? h * 60 + m : null;
  };
  const a = parse(begin);
  const b = parse(end);
  return a === null || b === null ? null : b - a;
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
  // The dropdowns get their items from the ranked resolver in stopResolver.ts (prefix matches
  // first, then word-start, then substring; busiest stops first; capped), so the full ~30k-row
  // list is never rendered. Passing a function as `items` also makes SearchableDropdown skip its
  // own label filter, so the resolver's ranking is what shows. New resolver identity per pool
  // change → the dropdowns re-resolve when the facet toggles.
  const items = useMemo(
    () => (text: string): Item[] =>
      rankStops(pool, text).map((s) => ({
        value: s.stop_place,
        label: stopLabel(s, ambiguous),
        icons: modeIcons(s.mode),
        itemKey: s.stop_place,
      })),
    [pool, ambiguous],
  );
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
  const [problem, setProblem] = useState<Problem | null>(null);
  const [serverError, setServerError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const fromRef = useRef<HTMLInputElement>(null);
  const toRef = useRef<HTMLInputElement>(null);
  const beginRef = useRef<HTMLInputElement>(null);
  const daysRef = useRef<HTMLFieldSetElement>(null);

  // Reset the form each time the modal opens.
  useEffect(() => {
    if (open) {
      setMode('alle');
      setFrom(null);
      setTo(null);
      setBegin('07:00');
      setEnd('09:00');
      setDays(DEFAULT_DAYS);
      setProblem(null);
      setServerError(null);
      setSaving(false);
    }
  }, [open]);

  // A problem clears as soon as the field it names changes — the reader fixed it, and a stale red
  // message under a now-valid field would make the form look broken.
  const clearProblem = (field: Field) => {
    setProblem((current) => (current?.field === field ? null : current));
  };

  function toggleDay(d: number) {
    clearProblem('days');
    setDays((prev) => (prev.includes(d) ? prev.filter((x) => x !== d) : [...prev, d].sort((a, b) => a - b)));
  }

  function validate(): Problem | null {
    if (!from) return { field: 'from', message: 'Du må velge hvor du reiser fra.' };
    if (!to) return { field: 'to', message: 'Du må velge hvor du reiser til.' };
    if (from.value === to.value) {
      return { field: 'to', message: 'Reisen må gå mellom to ulike stopp.' };
    }
    if (!begin || !end) return { field: 'time', message: 'Du må velge når reisen starter og slutter.' };
    if (begin >= end) return { field: 'time', message: 'Tidsrommet må starte før det slutter.' };
    if (days.length === 0) return { field: 'days', message: 'Du må velge minst én ukedag.' };
    return null;
  }

  function focusField(field: Field) {
    const target = { from: fromRef, to: toRef, time: beginRef, days: daysRef }[field].current;
    target?.focus();
  }

  async function submit() {
    const found = validate();
    if (found) {
      setProblem(found);
      focusField(found.field);
      return;
    }
    setSaving(true);
    setServerError(null);
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
      setServerError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  // Typing a stop and tapping away without picking from the list used to leave the field EMPTY:
  // Linje's combobox resets the input to the selected item on blur, and there was none. The first
  // suggestion is drawn highlighted and reads as chosen, so on blur it is (design review
  // 2026-09-01, question 47 — answered: adopt the first item). A click on a list item blurs the
  // input first and selects the clicked item afterwards, so that selection wins over this one.
  function adoptFirstMatch(
    event: FocusEvent<HTMLInputElement>,
    current: Item | null,
    set: (item: Item) => void,
  ) {
    const typed = event.target.value.trim();
    if (current || !typed) return;
    const [first] = items(typed);
    if (first) set(first);
  }

  // The return trip is the same two stops the other way round (finding 40).
  function swap() {
    const previousFrom = from;
    setFrom(to);
    setTo(previousFrom);
    setProblem(null);
  }

  const windowMinutes = minutesBetween(begin, end);
  const longWindow = windowMinutes !== null && windowMinutes > LONG_WINDOW_MIN;
  const fieldProps = (field: Field) =>
    problem?.field === field ? { variant: 'negative' as const, feedback: problem.message } : {};

  return (
    <Modal open={open} onDismiss={onClose} size="medium" title="Legg til reise">
      {/* A real <form>, so Enter submits from any field (finding 28): both buttons were
          type="submit" with no form around them, and a keyboard user had to tab past every field
          to reach «Lagre reise». `noValidate` because the messages below are ours. */}
      <form
        className="av-form"
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
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
          ref={fromRef}
          label="Fra"
          items={items}
          debounceTimeout={0}
          selectedItem={from}
          onChange={(item) => {
            clearProblem('from');
            setFrom(item);
          }}
          onBlur={(event) => adoptFirstMatch(event, from, setFrom)}
          placeholder="Søk etter stopp"
          clearable
          {...fieldProps('from')}
        />
        <div className="av-form-swap">
          <IconButton type="button" aria-label="Bytt om fra og til" onClick={swap}>
            <VerticalSwitchIcon aria-hidden="true" />
          </IconButton>
        </div>
        <SearchableDropdown
          ref={toRef}
          label="Til"
          items={items}
          debounceTimeout={0}
          selectedItem={to}
          onChange={(item) => {
            clearProblem('to');
            setTo(item);
          }}
          onBlur={(event) => adoptFirstMatch(event, to, setTo)}
          placeholder="Søk etter stopp"
          clearable
          {...fieldProps('to')}
        />

        <div className="av-form-times">
          <TextField
            ref={beginRef}
            label="Fra klokkeslett"
            type="time"
            value={begin}
            onChange={(e) => {
              clearProblem('time');
              setBegin(e.target.value);
            }}
            {...fieldProps('time')}
          />
          <TextField
            label="Til klokkeslett"
            type="time"
            value={end}
            onChange={(e) => {
              clearProblem('time');
              setEnd(e.target.value);
            }}
          />
        </div>
        {longWindow && (
          <SmallText as="p" className="av-form-hint">
            Et langt tidsrom gir flere varsler. Reiser du til og fra jobb, gir to reiser med hvert
            sitt tidsrom mer treffsikre varsler.
          </SmallText>
        )}

        <fieldset
          ref={daysRef}
          className="av-form-days"
          tabIndex={-1}
          aria-invalid={problem?.field === 'days' || undefined}
          aria-describedby={problem?.field === 'days' ? 'weekday-error' : undefined}
        >
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
          {problem?.field === 'days' && (
            <SmallText as="p" id="weekday-error" role="alert" className="av-field-error">
              {problem.message}
            </SmallText>
          )}
        </fieldset>

        {/* Only what the server said: a field problem is shown under its field above. */}
        {serverError && (
          <SmallAlertBox variant="negative" width="fluid">
            {serverError}
          </SmallAlertBox>
        )}

        {/* T3, directly above the buttons: what this form collects, why, and how to undo it.
            Registration stays confirmation-style — this is the information that makes tapping
            «Lagre reise» informed, not a consent checkbox. */}
        <RegistreringsNotis brandId={brandId} />

        <div className="av-form-actions">
          <SecondaryButton type="button" onClick={onClose} disabled={saving}>
            Avbryt
          </SecondaryButton>
          <PrimaryButton type="submit" loading={saving}>
            Lagre reise
          </PrimaryButton>
        </div>
      </form>
    </Modal>
  );
}
