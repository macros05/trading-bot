'use strict';

const API = (() => {
  const bust = () => `?_=${Date.now()}`;

  async function get(url) {
    const r = await fetch(url + bust());
    if (!r.ok) throw new Error(`HTTP ${r.status} — ${url}`);
    return r;
  }

  return {
    async status() {
      return (await get('/status')).json();
    },
    async trades() {
      return (await get('/trades')).json();
    },
    async logs() {
      return (await get('/logs')).text();
    },
  };
})();
