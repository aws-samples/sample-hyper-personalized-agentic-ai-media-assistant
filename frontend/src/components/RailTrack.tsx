import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { useDevice } from './device';
import './RailTrack.css';

/**
 * Horizontal scroll track with Netflix-style prev/next arrows.
 * Arrows show on hover for TV/desktop and only when the row actually overflows;
 * the left arrow hides at the start and the right hides at the end. Mobile uses
 * native touch swipe (no arrows).
 */
export function RailTrack({
  children,
  resetKey,
}: {
  children: ReactNode;
  /** When this changes, scroll resets to the start (e.g. content swapped). */
  resetKey?: string | number;
}) {
  const device = useDevice();
  const ref = useRef<HTMLDivElement>(null);
  const [scrollable, setScrollable] = useState(false);
  const [atStart, setAtStart] = useState(true);
  const [atEnd, setAtEnd] = useState(true);

  const update = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    setScrollable(el.scrollWidth - el.clientWidth > 2);
    setAtStart(el.scrollLeft <= 2);
    setAtEnd(el.scrollLeft + el.clientWidth >= el.scrollWidth - 2);
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    update();
    const raf = requestAnimationFrame(update); // after first paint
    const ro = new ResizeObserver(update);
    ro.observe(el);
    window.addEventListener('resize', update);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      window.removeEventListener('resize', update);
    };
  }, [update]);

  // When the content set changes (async load / persona switch), reset to the
  // start so the row is left-aligned and the left arrow stays hidden.
  useEffect(() => {
    const el = ref.current;
    if (el) el.scrollLeft = 0;
    update();
  }, [resetKey, update]);

  const page = (dir: 1 | -1) => {
    const el = ref.current;
    if (!el) return;
    el.scrollBy({ left: dir * el.clientWidth * 0.85, behavior: 'smooth' });
  };

  const showArrows = device === 'tv' && scrollable;

  return (
    <div className="rail-track-wrap">
      {showArrows && !atStart && (
        <button className="rail-arrow left" aria-label="Scroll left" onClick={() => page(-1)}>
          <ChevronLeft />
        </button>
      )}
      <div className="rail-track" ref={ref} onScroll={update}>
        {children}
      </div>
      {showArrows && !atEnd && (
        <button className="rail-arrow right" aria-label="Scroll right" onClick={() => page(1)}>
          <ChevronRight />
        </button>
      )}
    </div>
  );
}
