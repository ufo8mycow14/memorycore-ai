//! Synthetic compatibility check, not a replacement for the isolated broker.
use std::sync::Arc;
use tokio::sync::Semaphore;

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let db = tokio_rusqlite::Connection::open_in_memory().await?;
    db.call(|conn| -> rusqlite::Result<()> {
        conn.execute_batch(
            "CREATE TABLE synthetic(id INTEGER PRIMARY KEY, value INTEGER NOT NULL)",
        )?;
        #[cfg(feature = "sqlcipher")]
        assert!(
            !conn
                .query_row("PRAGMA cipher_version", [], |r| r.get::<_, String>(0))?
                .is_empty()
        );
        Ok(())
    })
    .await?;
    let admission = Arc::new(Semaphore::new(4));
    let mut tasks = tokio::task::JoinSet::new();
    for id in 0..10 {
        let db = db.clone();
        let admission = admission.clone();
        tasks.spawn(async move {
            let permit = admission.acquire_owned().await.unwrap();
            db.call(move |conn| -> rusqlite::Result<()> {
                // Hold admission until DB execution ends, even if the waiter is cancelled.
                let _permit = permit;
                let tx = conn.transaction()?;
                tx.execute("INSERT INTO synthetic VALUES(?,?)", [id, id])?;
                let value: i64 =
                    tx.query_row("SELECT value FROM synthetic WHERE id=?", [id], |r| r.get(0))?;
                assert_eq!(value, id);
                tx.commit()
            })
            .await
        });
    }
    while let Some(result) = tasks.join_next().await {
        result??;
    }
    let result = db
        .call(|conn| -> rusqlite::Result<(i64, i64)> {
            {
                let tx = conn.transaction()?;
                tx.execute("INSERT INTO synthetic VALUES(100,100)", [])?;
                // Dropping an uncommitted transaction must roll it back.
            }
            conn.query_row("SELECT count(*),sum(value) FROM synthetic", [], |r| {
                Ok((r.get(0)?, r.get(1)?))
            })
        })
        .await?;
    assert_eq!(result, (10, 45));
    db.close().await?;
    println!(
        "PASS: ten bounded async transactions, readback and rollback; synthetic in-memory only"
    );
    Ok(())
}
