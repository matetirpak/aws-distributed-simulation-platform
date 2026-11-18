//! Panics in game code are caught and reported by the caller, so a game bug that fires every
//! turn is logged once per match instead of flooding the logs (and the CloudWatch bill).

use std::cell::{Cell, RefCell};
use std::panic::{self, AssertUnwindSafe};

thread_local! {
    static QUIET: Cell<bool> = const { Cell::new(false) };
    static LAST: RefCell<Option<String>> = const { RefCell::new(None) };
}

/// Panics outside [`quietly`] are controller bugs and are logged right away.
pub(crate) fn install_hook() {
    panic::set_hook(Box::new(|info| {
        if QUIET.with(Cell::get) {
            LAST.with(|last| *last.borrow_mut() = Some(info.to_string()));
        } else {
            tracing::error!(panic = %info, "controller panicked");
        }
    }));
}

/// Runs game code, turning a panic into `Err(message)` without logging it.
pub(crate) fn quietly<T>(f: impl FnOnce() -> T) -> Result<T, String> {
    QUIET.with(|q| q.set(true));
    let result = panic::catch_unwind(AssertUnwindSafe(f));
    QUIET.with(|q| q.set(false));
    result.map_err(|_| {
        LAST.with(|last| last.borrow_mut().take())
            .unwrap_or_else(|| "unknown panic".into())
    })
}
