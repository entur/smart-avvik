// A minimal IndexedDB stand-in: one database, event-style requests that settle on a microtask,
// stores as Maps. Enough for the open → transaction → objectStore → get/put/delete shape that
// src/pushIdentity.ts and public/sw.js use, and nothing more. Shared by the two suites so the page
// side and the worker side are tested against the SAME fake — if the fake were wrong in a way that
// let the two disagree, both would still pass.

export function fakeIndexedDb(initial = {}) {
  const stores = new Map();
  for (const [name, entries] of Object.entries(initial)) {
    stores.set(name, new Map(Object.entries(entries)));
  }
  let created = stores.size > 0;
  const opens = [];
  let closes = 0;

  function request(run) {
    const req = { result: undefined, error: null, onsuccess: null, onerror: null };
    queueMicrotask(() => {
      try {
        req.result = run();
        if (req.onsuccess) req.onsuccess({ target: req });
      } catch (e) {
        req.error = e;
        if (req.onerror) req.onerror({ target: req });
      }
    });
    return req;
  }

  const db = {
    objectStoreNames: { contains: (name) => stores.has(name) },
    createObjectStore(name) {
      stores.set(name, new Map());
      return {};
    },
    transaction(name) {
      return {
        objectStore(storeName) {
          const map = stores.get(storeName);
          if (!map) throw new Error(`NotFoundError: no object store ${storeName}`);
          return {
            get: (key) => request(() => map.get(key)),
            put: (value, key) =>
              request(() => {
                map.set(key, value);
                return key;
              }),
            delete: (key) => request(() => void map.delete(key)),
          };
        },
      };
    },
    close() {
      closes += 1;
    },
  };

  return {
    stores,
    opens,
    get closes() {
      return closes;
    },
    open(name, version) {
      opens.push([name, version]);
      const req = { result: db, error: null, onupgradeneeded: null, onsuccess: null, onerror: null };
      queueMicrotask(() => {
        if (!created) {
          created = true;
          if (req.onupgradeneeded) req.onupgradeneeded({ target: req });
        }
        if (req.onsuccess) req.onsuccess({ target: req });
      });
      return req;
    },
  };
}
