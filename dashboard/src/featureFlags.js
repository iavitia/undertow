// Live third-party lookups (Google News RSS, Roll Call's Trump archive) are
// slow -- the RSS call isn't cached at all, and Trump-post windows outside
// the cached range paginate an external archive. Both were firing on
// ordinary navigation (selecting a signal, opening a catalyst). Flip this
// back on when we want them again; every call site below already checks it.
export const NEWS_LOOKUPS_ENABLED = false;
