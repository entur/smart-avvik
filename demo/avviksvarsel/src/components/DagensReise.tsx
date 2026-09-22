import { Fragment, useLayoutEffect, useRef, useState } from 'react';
import type { JourneyStatus } from '../api';
import { fmtTime } from '../format';
import { caption, circleClass, circleLabel, splitAtNow, updatedAt } from './dagensReiseModel';

/** «I dag»: one circle per train in today's window, left to right, filled for a fact and a ring for
 *  a forecast (plan 2026-09-22-001). Renders nothing on a day the journey is not travelled. */
export default function DagensReise({ today }: { today: JourneyStatus }) {
  const [selected, setSelected] = useState<string | null>(null);
  const rowRef = useRef<HTMLDivElement>(null);
  const nowRef = useRef<HTMLLIElement>(null);
  const past = splitAtNow(today.trains, today.now);

  // Park «nå» in the left third, so the trains still ahead are what the eye lands on. Only when the
  // marker MOVES, not on every 60 s refresh, or a traveller scrolling back would be yanked forward.
  // No smooth scroll: this is placement on arrival, not motion.
  useLayoutEffect(() => {
    const row = rowRef.current;
    const marker = nowRef.current;
    if (row && marker) row.scrollLeft = Math.max(0, marker.offsetLeft - row.clientWidth / 3);
  }, [past]);

  if (today.reason === 'not_a_travel_day' || today.window === null) return null;

  const updated = updatedAt(today);
  const chosen = today.trains.find((t) => t.jkey === selected) ?? null;
  const marker = (
    <li className="dr-now" ref={nowRef} aria-hidden="true">
      <span>nå</span>
    </li>
  );

  return (
    <section className="dr" aria-label="Dagens avganger">
      <div className="dr-head">
        <span className="dr-title">I dag</span>
        {updated && <span className="dr-updated">Oppdatert {fmtTime(updated)}</span>}
      </div>
      {today.trains.length > 0 && (
        <div className="dr-row" ref={rowRef}>
          <ol className="dr-track">
            {today.trains.map((train, i) => (
              <Fragment key={train.jkey}>
                {i === past && marker}
                <li className="dr-stop">
                  <button
                    type="button"
                    className={circleClass(train)}
                    aria-label={circleLabel(train, today.now)}
                    aria-pressed={selected === train.jkey}
                    onClick={() => setSelected(selected === train.jkey ? null : train.jkey)}
                  >
                    <span className="dr-face">{fmtTime(train.departure_time)}</span>
                  </button>
                </li>
              </Fragment>
            ))}
            {past === today.trains.length && marker}
          </ol>
        </div>
      )}
      <p className="dr-legend">
        <span className="dr-key dr-key-fact" aria-hidden="true" /> Bekreftet
        <span className="dr-key dr-key-ring" aria-hidden="true" /> Prognose · {caption(today)}
      </p>
      {chosen && (
        <p className="dr-detail" aria-live="polite">
          {circleLabel(chosen, today.now)}
        </p>
      )}
    </section>
  );
}
