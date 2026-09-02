// The traffic-light vocabulary: label, adjective and one sentence per level.
//
// A guarded DUPLICATE of demo/avviksvarsel/src/levels.ts, for the same reason personvern.ts is
// duplicated: each front-end is its own npm app and the image's node stage copies only its own
// directory, so a relative import into the other app resolves on a laptop and fails the image
// build. disruptions/api/test_togvarsel.py asserts the two copies are byte-identical and that the
// sentences match the server's togvarsel._LEVEL_TEXT, which the persisted feedback snapshot quotes.
//
// «GRØNT / GULT / RØDT», the neuter form, because every sentence the label sits in is about «nivå»
// -- /varsel said GRØNT while /togmelding said GRØNN for one concept (design review 2026-09-01,
// finding 12). And the middle sentence is on the same axis as the two beside it (finding 11): «noe
// usikkert» measured something else, so a reader could not place it between them.

export const LEVEL_LABEL: Record<string, string> = {
  green: 'GRØNT',
  yellow: 'GULT',
  red: 'RØDT',
};

/** The level as an adjective on «nivå»: «gult nivå». Also the push titles' word. */
export const LEVEL_ADJ: Record<string, string> = {
  green: 'grønt',
  yellow: 'gult',
  red: 'rødt',
};

export const LEVEL_TEXT: Record<string, string> = {
  green: 'liten fare for forstyrrelser',
  yellow: 'moderat fare for forstyrrelser',
  red: 'stor fare for forstyrrelser',
};

/** «Morgenrushet 07:00–09:00» from the server's lower-case scope, for a heading. */
export function capitalize(text: string): string {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}
