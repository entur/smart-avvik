import { useEffect, useMemo, useRef, useState } from 'react';
import type { DemoStop } from './api';

// Searchable stop picker: with bus included the stop list runs to thousands of entries, so a
// plain <select> is unusable. Plain-React combobox: type to filter (prefix matches first, then
// substring, busiest stops first), arrow keys + Enter to pick, Escape/blur reverts to the
// current selection. Renders at most MAX_SHOWN matches — keep typing to narrow.

const MAX_SHOWN = 60;

function rankMatches(stops: DemoStop[], query: string): DemoStop[] {
  const q = query.trim().toLowerCase();
  const scored: { stop: DemoStop; rank: number }[] = [];
  for (const s of stops) {
    const name = (s.stop_name ?? s.stop_place).toLowerCase();
    if (q === '') {
      scored.push({ stop: s, rank: 2 });
      continue;
    }
    const idx = name.indexOf(q);
    if (idx === -1) continue;
    // prefix match > start of a later word > anywhere in the name
    const rank = idx === 0 ? 0 : name[idx - 1] === ' ' || name[idx - 1] === '(' ? 1 : 2;
    scored.push({ stop: s, rank });
  }
  scored.sort((a, b) => a.rank - b.rank || b.stop.n_calls - a.stop.n_calls);
  return scored.slice(0, MAX_SHOWN).map((x) => x.stop);
}

export default function StopCombobox({
  id,
  stops,
  value,
  onChange,
  placeholder,
}: {
  id: string;
  stops: DemoStop[];
  value: string; // selected stop_place, '' = none
  onChange: (stopPlace: string) => void;
  placeholder?: string;
}) {
  const byPlace = useMemo(() => new Map(stops.map((s) => [s.stop_place, s])), [stops]);
  const selectedName = value ? (byPlace.get(value)?.stop_name ?? value) : '';
  const [query, setQuery] = useState(selectedName);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  // Reflect selections made outside the combobox (example scenario, swap button).
  useEffect(() => {
    setQuery(selectedName);
  }, [selectedName]);

  const matches = useMemo(
    () => (open ? rankMatches(stops, query) : []),
    [open, stops, query],
  );

  function pick(stop: DemoStop) {
    onChange(stop.stop_place);
    setQuery(stop.stop_name ?? stop.stop_place);
    setOpen(false);
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (!open && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
      setOpen(true);
      return;
    }
    if (!open) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActive((a) => Math.min(a + 1, matches.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === 'Enter') {
      if (matches[active]) {
        e.preventDefault();
        pick(matches[active]);
      }
    } else if (e.key === 'Escape') {
      setOpen(false);
      setQuery(selectedName);
    }
  }

  return (
    <div className="combobox">
      <input
        id={id}
        ref={inputRef}
        type="text"
        role="combobox"
        aria-expanded={open}
        aria-autocomplete="list"
        aria-controls={`${id}-listbox`}
        autoComplete="off"
        placeholder={placeholder}
        value={query}
        onFocus={() => {
          setOpen(true);
          setActive(0);
          inputRef.current?.select();
        }}
        onBlur={() => {
          // Option clicks use onMouseDown (fires before blur), so a plain blur means
          // "abandoned" — revert to the current selection.
          setOpen(false);
          setQuery(selectedName);
        }}
        onChange={(e) => {
          setQuery(e.target.value);
          setOpen(true);
          setActive(0);
        }}
        onKeyDown={onKeyDown}
      />
      {open && matches.length > 0 && (
        <ul className="combobox-list" id={`${id}-listbox`} role="listbox">
          {matches.map((s, i) => (
            <li
              key={s.stop_place}
              role="option"
              aria-selected={s.stop_place === value}
              className={`combobox-option${i === active ? ' active' : ''}`}
              onMouseDown={(e) => {
                e.preventDefault();
                pick(s);
              }}
              onMouseEnter={() => setActive(i)}
            >
              <span className="option-name">{s.stop_name ?? s.stop_place}</span>
              <span className="option-mode">{s.mode}</span>
            </li>
          ))}
          {matches.length === MAX_SHOWN && (
            <li className="combobox-more">Flere treff — fortsett å skrive for å avgrense</li>
          )}
        </ul>
      )}
      {open && matches.length === 0 && query.trim() !== '' && (
        <ul className="combobox-list" id={`${id}-listbox`} role="listbox">
          <li className="combobox-more">Ingen stopp funnet</li>
        </ul>
      )}
    </div>
  );
}
