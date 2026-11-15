import { useEffect, useMemo, useRef, useState } from 'react';
import { Canvas, useFrame, useThree, type ThreeEvent } from '@react-three/fiber';
import { Html, Line, OrbitControls } from '@react-three/drei';
import * as THREE from 'three';
import type { OrbitControls as OrbitControlsImpl } from 'three-stdlib';
import type { Layout, Neighbor, Taste, Vec3 } from './api';
import { countLabel } from './Panels';

export const CLUSTER_COLORS = [
  '#4cc9f0', '#f72585', '#ffd166', '#06d6a0', '#a78bfa', '#fb8500', '#90e0ef', '#ef476f', '#b5e48c',
  '#f4a261', '#c77dff', '#48cae4', '#ffafcc', '#80ed99', '#e9c46a', '#ff6b6b', '#64dfdf', '#bdb2ff',
];
export const TYPE_COLORS: Record<string, string> = {
  movie: '#4cc9f0', tvSeries: '#ffd166', tvEpisode: '#f4a261', tvMiniSeries: '#fb8500',
  'micro-drama': '#f72585', videoGame: '#80ed99', video: '#a78bfa', tvMovie: '#90e0ef', short: '#c77dff',
  tvSpecial: '#e9c46a',
};

export interface Overlay {
  name: string;
  color: string;
  taste: Taste;
}

/** A free-floating marker (e.g. a what-if taste vector); glides when `pos` changes. */
export interface Marker {
  key: string;
  pos: Vec3;
  color: string;
  label?: string;
  sub?: string;
  size?: number;
  ghost?: boolean;
  lines?: string[];        // title ids to draw a line to
}

interface Props {
  layout: Layout;
  colorBy: 'cluster' | 'type';
  overlays: Overlay[];
  selected: string | null;
  selectedNeighbors: Neighbor[];
  focusCluster: number | null;
  showLabels: boolean;
  onSelect: (id: string | null) => void;
  highlights?: Map<string, string>;   // title id -> colour (what-if stages)
  ranks?: Map<string, string>;        // title id -> short label (e.g. "#1")
  markers?: Marker[];
}

const VERT = `
  attribute float size;
  attribute vec3 color;
  attribute float alpha;
  varying vec3 vColor;
  varying float vAlpha;
  void main() {
    vColor = color;
    vAlpha = alpha;
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    gl_PointSize = size * (420.0 / -mv.z);
    gl_Position = projectionMatrix * mv;
  }`;
const FRAG = `
  varying vec3 vColor;
  varying float vAlpha;
  void main() {
    vec2 c = gl_PointCoord - 0.5;
    float d = length(c);
    if (d > 0.5) discard;
    float edge = smoothstep(0.5, 0.32, d);
    gl_FragColor = vec4(vColor * (0.75 + 0.5 * (0.5 - d)), vAlpha * edge);
  }`;

function TitlePoints({ layout, colorBy, overlays, selected, selectedNeighbors, focusCluster, onSelect, highlights }: Props) {
  const [hover, setHover] = useState<number | null>(null);
  const geom = useMemo(() => {
    const g = new THREE.BufferGeometry();
    const pos = new Float32Array(layout.titles.length * 3);
    layout.titles.forEach((t, i) => pos.set(t.pos, i * 3));
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    g.setAttribute('color', new THREE.BufferAttribute(new Float32Array(layout.titles.length * 3), 3));
    g.setAttribute('size', new THREE.BufferAttribute(new Float32Array(layout.titles.length), 1));
    g.setAttribute('alpha', new THREE.BufferAttribute(new Float32Array(layout.titles.length), 1));
    g.computeBoundingSphere();
    return g;
  }, [layout]);

  // Recolour / resize on every relevant state change.
  useEffect(() => {
    const color = geom.getAttribute('color') as THREE.BufferAttribute;
    const size = geom.getAttribute('size') as THREE.BufferAttribute;
    const alpha = geom.getAttribute('alpha') as THREE.BufferAttribute;
    const highlight = new Map<string, string>(highlights ?? []);
    for (const o of overlays) {
      for (const w of o.taste.watched) highlight.set(w.id, o.color);
      for (const v of o.taste.taste_vectors) for (const n of v.neighbors) if (!highlight.has(n.id)) highlight.set(n.id, o.color);
    }
    const nearSel = new Set(selectedNeighbors.map((n) => n.id));
    // A selected title or focused cluster dims everything else hard. A viewer
    // overlay only recedes the rest a little, so the catalog keeps its shape
    // and the overlay reads as "where this taste sits in the same space".
    const strongDim = selected !== null || focusCluster !== null || (highlights?.size ?? 0) > 0;
    const dimming = strongDim || overlays.length > 0 || highlight.size > 0;
    const c = new THREE.Color();
    layout.titles.forEach((t, i) => {
      c.set(colorBy === 'cluster' ? CLUSTER_COLORS[t.cluster % CLUSTER_COLORS.length] : TYPE_COLORS[t.type] ?? '#9aa4b2');
      let s = t.type === 'micro-drama' ? 2.4 : 1.25;
      let a = 0.85;
      const lit = t.id === selected || nearSel.has(t.id) || highlight.has(t.id) || (focusCluster !== null && t.cluster === focusCluster);
      if (dimming && !lit) {
        if (strongDim) {
          a = 0.16;
          s *= 0.85;
        } else {
          a = 0.45;
        }
      }
      if (highlight.has(t.id)) {
        c.set(highlight.get(t.id)!);
        s = 2.6;
        a = 1;
      }
      if (nearSel.has(t.id)) { s = 2.6; a = 1; }
      if (t.id === selected) { c.set('#ffffff'); s = 4.2; a = 1; }
      if (i === hover) { s = Math.max(s, 3.4); a = 1; }
      color.setXYZ(i, c.r, c.g, c.b);
      size.setX(i, s);
      alpha.setX(i, a);
    });
    color.needsUpdate = size.needsUpdate = alpha.needsUpdate = true;
  }, [geom, layout, colorBy, overlays, selected, selectedNeighbors, focusCluster, hover, highlights]);

  const material = useMemo(
    () => new THREE.ShaderMaterial({ vertexShader: VERT, fragmentShader: FRAG, transparent: true, depthWrite: false }),
    [],
  );
  const t = hover !== null ? layout.titles[hover] : null;

  return (
    <group>
      <points
        geometry={geom}
        material={material}
        onPointerMove={(e: ThreeEvent<PointerEvent>) => {
          e.stopPropagation();
          setHover(e.index ?? null);
        }}
        onPointerOut={() => setHover(null)}
        onClick={(e: ThreeEvent<MouseEvent>) => {
          e.stopPropagation();
          if (e.index !== undefined) onSelect(layout.titles[e.index].id);
        }}
      />
      {t && (
        <Html position={t.pos} style={{ pointerEvents: 'none' }} zIndexRange={[20, 10]}>
          <div className="tip">
            <b>{t.name}</b>
            <span>
              {[t.year, t.type === 'micro-drama' ? 'microdrama' : t.type, t.genres].filter(Boolean).join(' · ')}
            </span>
            <span className="tip-cluster">{layout.clusters.find((c) => c.id === t.cluster)?.label}</span>
          </div>
        </Html>
      )}
    </group>
  );
}

function TasteAnchors({ overlays, layout }: { overlays: Overlay[]; layout: Layout }) {
  const ref = useRef<THREE.Group>(null);
  useFrame(({ clock }) => {
    const k = 1 + 0.12 * Math.sin(clock.elapsedTime * 2.2);
    ref.current?.children.forEach((g) => {
      const halo = g.children[1];
      if (halo) halo.scale.setScalar(k);
    });
  });
  return (
    <group ref={ref}>
      {overlays.flatMap((o) =>
        o.taste.taste_vectors
          .filter((v) => v.position)
          .map((v) => {
            const p = v.position as Vec3;
            const r = 1.1 + 2.2 * Math.sqrt(v.weight);
            return (
              <group key={`${o.taste.profile_id}-${v.index}`} position={p}>
                <mesh>
                  <sphereGeometry args={[r, 32, 32]} />
                  <meshBasicMaterial color={o.color} />
                </mesh>
                <mesh>
                  <sphereGeometry args={[r * 1.9, 32, 32]} />
                  <meshBasicMaterial color={o.color} transparent opacity={0.16} depthWrite={false} />
                </mesh>
                {v.neighbors.map((n) => {
                  const i = layout.index.get(n.id);
                  if (i === undefined) return null;
                  const q = layout.titles[i].pos;
                  return (
                    <Line key={n.id} points={[[0, 0, 0], [q[0] - p[0], q[1] - p[1], q[2] - p[2]]]}
                      color={o.color} lineWidth={1} transparent opacity={0.45} />
                  );
                })}
                <Html center position={[0, r * 1.9 + 3.2, 0]} style={{ pointerEvents: 'none' }} zIndexRange={[30, 20]}>
                  <div className="anchor-label" style={{ borderColor: o.color }}>
                    <div><b style={{ color: o.color }}>{o.name}</b> · taste vector {v.index + 1}</div>
                    <span>{countLabel(v.count, o.taste.taste_source)} · weight {Math.round(v.weight * 100)}%</span>
                  </div>
                </Html>
              </group>
            );
          }),
      )}
    </group>
  );
}

/** A marker that glides to its target position (taste vectors moving in a what-if replay). */
function GlidingMarker({ m, layout }: { m: Marker; layout: Layout }) {
  const ref = useRef<THREE.Group>(null);
  const halo = useRef<THREE.Mesh>(null);
  const [at, setAt] = useState<Vec3>(m.pos);
  const reduce = useMemo(() => window.matchMedia('(prefers-reduced-motion: reduce)').matches, []);
  useFrame(({ clock }) => {
    const g = ref.current;
    if (!g) return;
    const goal = new THREE.Vector3(...m.pos);
    if (reduce) g.position.copy(goal);
    else g.position.lerp(goal, 0.06);
    if (halo.current && !m.ghost) halo.current.scale.setScalar(1 + 0.12 * Math.sin(clock.elapsedTime * 2.2));
    if (g.position.distanceToSquared(new THREE.Vector3(...at)) > 0.25) setAt([g.position.x, g.position.y, g.position.z]);
  });
  const r = m.size ?? 2.4;
  return (
    <group>
      <group ref={ref} position={at}>
        <mesh>
          <sphereGeometry args={[r, 32, 32]} />
          <meshBasicMaterial color={m.color} transparent={m.ghost} opacity={m.ghost ? 0.35 : 1} depthWrite={!m.ghost} />
        </mesh>
        {!m.ghost && (
          <mesh ref={halo}>
            <sphereGeometry args={[r * 1.9, 32, 32]} />
            <meshBasicMaterial color={m.color} transparent opacity={0.16} depthWrite={false} />
          </mesh>
        )}
        {m.label && (
          <Html center position={[0, r * 1.9 + 3.2, 0]} style={{ pointerEvents: 'none' }} zIndexRange={[30, 20]}>
            <div className={`anchor-label${m.ghost ? ' ghost' : ''}`} style={{ borderColor: m.color }}>
              <div><b style={{ color: m.color }}>{m.label}</b></div>
              {m.sub && <span>{m.sub}</span>}
            </div>
          </Html>
        )}
      </group>
      {(m.lines ?? []).map((id) => {
        const i = layout.index.get(id);
        if (i === undefined) return null;
        return <Line key={id} points={[at, layout.titles[i].pos]} color={m.color} lineWidth={1} transparent opacity={0.4} />;
      })}
    </group>
  );
}

function RankLabels({ layout, ranks }: { layout: Layout; ranks: Map<string, string> }) {
  return (
    <group>
      {[...ranks.entries()].map(([id, text]) => {
        const i = layout.index.get(id);
        if (i === undefined) return null;
        return (
          <Html key={id} position={layout.titles[i].pos} style={{ pointerEvents: 'none' }} zIndexRange={[25, 15]}>
            <div className="rank-label">{text}</div>
          </Html>
        );
      })}
    </group>
  );
}

function SelectionLines({ layout, selected, neighbors }: { layout: Layout; selected: string | null; neighbors: Neighbor[] }) {
  if (!selected) return null;
  const i = layout.index.get(selected);
  if (i === undefined) return null;
  const p = layout.titles[i].pos;
  return (
    <group>
      {neighbors.map((n) => {
        const j = layout.index.get(n.id);
        if (j === undefined) return null;
        return <Line key={n.id} points={[p, layout.titles[j].pos]} color="#ffffff" lineWidth={1} transparent opacity={0.55} />;
      })}
    </group>
  );
}

function ClusterLabels({ layout, focusCluster }: { layout: Layout; focusCluster: number | null }) {
  return (
    <group>
      {layout.clusters.map((c) => (
        <Html key={c.id} position={c.center} center style={{ pointerEvents: 'none' }} zIndexRange={[5, 0]}>
          <div className={`cluster-label${focusCluster === c.id ? ' on' : ''}`}
            style={{ color: CLUSTER_COLORS[c.id % CLUSTER_COLORS.length] }}>
            {c.label.replace(/ \(like .*\)$/, '')}
          </div>
        </Html>
      ))}
    </group>
  );
}

/** Default framing: the whole catalog. Matches the Canvas camera below. */
export const HOME_DISTANCE = Math.hypot(25, 115);

/**
 * Smoothly moves the orbit target to a point of interest and, when `distance`
 * is given, the camera to that distance from it along the current viewing
 * direction -- so focusing a viewer's taste flies in toward it instead of
 * sliding sideways at full-map distance. A null distance keeps the current one.
 */
function CameraRig({ target, distance }: { target: Vec3 | null; distance: number | null }) {
  const controls = useThree((s) => s.controls) as unknown as OrbitControlsImpl | null;
  const camera = useThree((s) => s.camera);
  const goal = useRef<{ target: THREE.Vector3; distance: number | null } | null>(null);
  useEffect(() => {
    goal.current = {
      target: target ? new THREE.Vector3(...target) : new THREE.Vector3(0, 0, 0),
      distance,
    };
  }, [target, distance]);
  useFrame(() => {
    if (!controls || !goal.current) return;
    const move = goal.current.target.clone().sub(controls.target).multiplyScalar(0.08);
    const offset = camera.position.clone().sub(controls.target);
    const dist = offset.length();
    const want = goal.current.distance ?? dist;
    if (move.lengthSq() < 1e-4 && Math.abs(want - dist) < 0.05) {
      goal.current = null;
      return;
    }
    controls.target.add(move);
    camera.position.copy(controls.target).add(offset.setLength(dist + (want - dist) * 0.08));
    controls.update();
  });
  return null;
}

export function MapScene(props: Props & { target: Vec3 | null; distance?: number | null; autoRotate: boolean }) {
  return (
    <Canvas camera={{ position: [0, 25, 115], fov: 50, near: 0.1, far: 2000 }}
      raycaster={{ params: { Points: { threshold: 0.7 } } as THREE.Raycaster['params'] }}
      onPointerMissed={() => props.onSelect(null)} dpr={[1, 2]}>
      <color attach="background" args={['#070a12']} />
      <fog attach="fog" args={['#070a12', 140, 320]} />
      <TitlePoints {...props} />
      <TasteAnchors overlays={props.overlays} layout={props.layout} />
      {(props.markers ?? []).map((m) => <GlidingMarker key={m.key} m={m} layout={props.layout} />)}
      {props.ranks && <RankLabels layout={props.layout} ranks={props.ranks} />}
      <SelectionLines layout={props.layout} selected={props.selected} neighbors={props.selectedNeighbors} />
      {props.showLabels && <ClusterLabels layout={props.layout} focusCluster={props.focusCluster} />}
      <OrbitControls makeDefault enableDamping dampingFactor={0.08} autoRotate={props.autoRotate} autoRotateSpeed={0.35}
        minDistance={8} maxDistance={260} />
      <CameraRig target={props.target} distance={props.distance ?? null} />
    </Canvas>
  );
}
