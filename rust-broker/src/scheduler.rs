//! FIFO waiting with idle-reader affinity; a lease returns its slot on cancellation.
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};
use tokio::sync::{Mutex, mpsc};

pub struct ReadPool {
    sender: mpsc::Sender<usize>,
    receiver: Mutex<mpsc::Receiver<usize>>,
    live: Arc<AtomicUsize>,
}
pub struct ReadLease {
    pub index: usize,
    sender: mpsc::Sender<usize>,
    live: Arc<AtomicUsize>,
    retired: bool,
}
impl ReadPool {
    pub fn new(size: usize) -> Self {
        assert!(size > 0);
        let (sender, receiver) = mpsc::channel(size);
        for index in 0..size {
            sender.try_send(index).unwrap();
        }
        Self {
            sender,
            receiver: Mutex::new(receiver),
            live: Arc::new(AtomicUsize::new(size)),
        }
    }
    pub async fn acquire(&self) -> Option<ReadLease> {
        self.acquire_preferred(None).await
    }
    pub async fn acquire_preferred(&self, preferred: Option<usize>) -> Option<ReadLease> {
        let mut receiver = self.receiver.lock().await;
        let mut index = receiver.recv().await.expect("pool retains sender");
        if index == usize::MAX {
            let _ = self.sender.try_send(index);
            return None;
        }
        if preferred.is_some_and(|preferred| preferred != index) {
            let mut idle = vec![index];
            while let Ok(available) = receiver.try_recv() {
                idle.push(available);
            }
            let position = idle
                .iter()
                .position(|index| Some(*index) == preferred)
                .unwrap_or(0);
            index = idle.remove(position);
            for available in idle {
                self.sender
                    .try_send(available)
                    .expect("one slot per live reader");
            }
        }
        Some(ReadLease {
            index,
            sender: self.sender.clone(),
            live: self.live.clone(),
            retired: false,
        })
    }
}
impl ReadLease {
    pub fn retire(&mut self) {
        if !self.retired {
            self.retired = true;
            if self.live.fetch_sub(1, Ordering::AcqRel) == 1 {
                let _ = self.sender.try_send(usize::MAX);
            }
        }
    }
}
impl Drop for ReadLease {
    fn drop(&mut self) {
        if !self.retired {
            let _ = self.sender.try_send(self.index);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn affinity_reuses_idle_readers_without_waiting_for_busy_or_retired_ones() {
        let pool = ReadPool::new(3);
        for _ in 0..3 {
            assert_eq!(pool.acquire_preferred(Some(2)).await.unwrap().index, 2);
        }
        let preferred = pool.acquire_preferred(Some(2)).await.unwrap();
        let fallback = tokio::time::timeout(
            std::time::Duration::from_millis(50),
            pool.acquire_preferred(Some(2)),
        )
        .await
        .unwrap()
        .unwrap();
        assert_ne!(fallback.index, preferred.index);
        drop(fallback);
        drop(preferred);
        pool.acquire_preferred(Some(2)).await.unwrap().retire();
        let first = pool.acquire_preferred(Some(2)).await.unwrap();
        let second = pool.acquire_preferred(Some(2)).await.unwrap();
        assert_ne!(first.index, 2);
        assert_ne!(second.index, 2);
        assert_ne!(first.index, second.index);
    }

    #[tokio::test]
    async fn cancelled_affinity_wait_does_not_lose_a_reader() {
        let pool = ReadPool::new(1);
        let held = pool.acquire().await.unwrap();
        assert!(
            tokio::time::timeout(
                std::time::Duration::from_millis(5),
                pool.acquire_preferred(Some(0))
            )
            .await
            .is_err()
        );
        drop(held);
        assert_eq!(pool.acquire_preferred(Some(0)).await.unwrap().index, 0);
    }

    #[tokio::test]
    async fn skips_busy_reader_and_returns_cancelled_lease() {
        let pool = ReadPool::new(2);
        let slow = pool.acquire().await.unwrap();
        let fast = pool.acquire().await.unwrap();
        let free = fast.index;
        drop(fast);
        let next = tokio::time::timeout(std::time::Duration::from_millis(50), pool.acquire())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(next.index, free);
        assert_ne!(next.index, slow.index);
        drop(next);
        drop(slow);
        assert_ne!(
            pool.acquire().await.unwrap().index,
            pool.acquire().await.unwrap().index
        );
    }
    #[tokio::test]
    async fn retired_readers_are_not_reassigned() {
        let pool = ReadPool::new(2);
        pool.acquire().await.unwrap().retire();
        let mut last = pool.acquire().await.unwrap();
        last.retire();
        drop(last);
        for _ in 0..3 {
            assert!(
                tokio::time::timeout(std::time::Duration::from_millis(50), pool.acquire())
                    .await
                    .unwrap()
                    .is_none()
            );
        }
    }
}
