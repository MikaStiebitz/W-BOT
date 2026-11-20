import { useState } from "react";
import { api } from "../api";
import { useStore } from "../store";

/**
 * The marker magazine — three bays, AMS style.
 *
 * Clicking a bay rotates the revolver to it; the colour and name describe the
 * marker physically loaded, and everything downstream (colour separation,
 * previews, sketching) works with exactly these colours. A disabled bay is an
 * empty one: separation skips it and selecting it is refused.
 */
export function AmsPanel() {
  const { ams, status, run, busy, loadAms } = useStore();
  const [editing, setEditing] = useState<number | null>(null);

  const select = (index: number) =>
    run(async () => {
      await api.amsSelect(index);
      await loadAms();
      await useStore.getState().refreshStatus();
    });

  const update = (index: number,
                  patch: { name?: string; hex?: string; enabled?: boolean }) =>
    run(async () => {
      await api.amsSetSlot({ index, ...patch });
      await loadAms();
    });

  if (!ams.length) return null;

  return (
    <section className="ams" aria-label="Marker-Magazin">
      <div className="ams-head">
        <span className="ams-title">Magazin</span>
        <span className="ams-hint">Klick = Slot anwählen</span>
      </div>
      <div className="ams-slots">
        {ams.map((slot) => {
          const active = status?.color === slot.index;
          return (
            <div key={slot.index}
                 className={`ams-slot${active ? " active" : ""}${slot.enabled ? "" : " disabled"}`}>
              <button
                className="ams-swatch"
                style={{ background: slot.enabled ? slot.hex : "transparent" }}
                onClick={() => slot.enabled && select(slot.index)}
                disabled={busy || !slot.enabled}
                title={slot.enabled
                  ? `Slot ${slot.index + 1} (${slot.name}) anwählen`
                  : `Slot ${slot.index + 1} ist leer`}
              >
                {!slot.enabled && <span className="ams-empty">leer</span>}
                {active && slot.enabled && <span className="ams-active-dot" />}
              </button>
              <div className="ams-slot-label">
                <b>{slot.index + 1}</b> {slot.name}
              </div>
              <button className="ams-edit"
                      onClick={() => setEditing(editing === slot.index ? null : slot.index)}
                      aria-label={`Slot ${slot.index + 1} bearbeiten`}>
                {editing === slot.index ? "✓" : "✎"}
              </button>
              {editing === slot.index && (
                <div className="ams-editor">
                  <input type="color" defaultValue={slot.hex}
                         aria-label="Markerfarbe"
                         onChange={(e) => update(slot.index, { hex: e.target.value })} />
                  <input type="text" defaultValue={slot.name} maxLength={20}
                         aria-label="Markername"
                         onBlur={(e) => {
                           const value = e.target.value.trim();
                           if (value && value !== slot.name)
                             void update(slot.index, { name: value });
                         }} />
                  <label className="ams-enabled">
                    <input type="checkbox" checked={slot.enabled}
                           onChange={(e) => update(slot.index, { enabled: e.target.checked })} />
                    bestückt
                  </label>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
