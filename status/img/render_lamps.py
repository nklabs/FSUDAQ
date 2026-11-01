"""Pilot lamps for the status page's Console skin, raytraced once (Cycles).

    blender -b --factory-startup -P render_lamps.py -- [OUTDIR] [SIZE] [SAMPLES] [POWER] [VARIANTS] [ONLY] [PLATE]
                                                        [DENSITY] [TINT_LENS] [TINT_LED] [BLOOM] [LED_R] [LENS_ROUGH]

With no arguments it writes the page's lamps (img/) next to this script. Each lamp: a flat diffuser lit from
below, under a solid dome of frosted acrylic that scatters the light, so the lens glows evenly in the lamp's
own colour with a gentle fall-off to the rim: no hot spot and no reflections on a lit lens, so one state is
never mistaken for another (an earlier bright centre made red read as amber). A dark collar and a satin-metal
ring with faint machining marks, in a panel of the Console skin's graphite, which a soft bloom of the lamp's
light falls on. Small imperfections: the ring's finish varies a little, a few faint specks of dust on the lens.
Variants differ only to a close look (the source's place within 0.02, brightness within 3%, the frosting and
dust; a fixed seed per state and variant). Power is balanced by how bright each colour looks (amber a little
more, it looked dull). The unlit lamp keeps a faint sheen, so it reads as glass. The image is square and opaque
(lamp and panel); the page fades its edges into the real panel. Files: lamp_<state>_<variant>.png for ok,
notice, problem, warn, bad and off. PLATE: the panel's colour, '#rrggbb' (default: the Console panel)."""
import math
import os
import random
import sys

import bmesh
import bpy

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
OUT = argv[0] if argv else os.path.dirname(os.path.abspath(__file__))
SIZE = int(argv[1]) if len(argv) > 1 else 384
SAMPLES = int(argv[2]) if len(argv) > 2 else 384
POWER = float(argv[3]) if len(argv) > 3 else 0.30
VARIANTS = int(argv[4]) if len(argv) > 4 else 3
ONLY = argv[5].split(",") if len(argv) > 5 and argv[5] else None
PLATE = argv[6] if len(argv) > 6 and argv[6] else "#262a30"
DENSITY = float(argv[7]) if len(argv) > 7 else 6.0    # how much the acrylic scatters
TINT_LENS = float(argv[8]) if len(argv) > 8 else 0.9  # how coloured the acrylic is (1: the lamp's colour)
TINT_LED = float(argv[9]) if len(argv) > 9 else 1.0
BLOOM = float(argv[10]) if len(argv) > 10 else 0.45
LED_R = float(argv[11]) if len(argv) > 11 else 0.6   # the light source: above 0.1 a flat diffuser of this radius under the lens, else an LED die
LENS_ROUGH = float(argv[12]) if len(argv) > 12 else 0.9   # frosting: rougher hides the LED die more
FRAME = 3.4                      # the image spans this many bezel radii... (ortho scale; the bezel is 2.0 across)
os.makedirs(OUT, exist_ok=True)

STATES = {"ok": "#36d158", "notice": "#3d8cf0", "problem": "#ff5fae", "warn": "#ffb81f", "bad": "#ff3b2f", "off": None}
TRIM = {"warn": 1.3}   # amber looked dull beside the others at the same balanced power


def lin(h):
    def c(v):
        v /= 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return (c(int(h[1:3], 16)), c(int(h[3:5], 16)), c(int(h[5:7], 16)), 1.0)


def luminance(c):
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


REF_Y = (luminance(lin("#3d8cf0")) + luminance(lin("#ff5fae"))) / 2      # the blue and pink lamps' brightness

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.engine = "CYCLES"
cy = scene.cycles
cy.device = "CPU"; cy.samples = SAMPLES; cy.use_denoising = True
cy.max_bounces = 32; cy.diffuse_bounces = 12; cy.glossy_bounces = 12; cy.transmission_bounces = 24; cy.volume_bounces = 4
cy.transparent_max_bounces = 8; cy.caustics_refractive = True; cy.caustics_reflective = True
cy.sample_clamp_indirect = 30.0
scene.render.resolution_x = scene.render.resolution_y = SIZE
scene.render.film_transparent = False
scene.render.image_settings.file_format = "PNG"; scene.render.image_settings.color_mode = "RGB"
scene.view_settings.view_transform = "Standard"; scene.view_settings.look = "None"

world = bpy.data.worlds.new("world"); scene.world = world; world.use_nodes = True
nt = world.node_tree
coord = nt.nodes.new("ShaderNodeTexCoord"); sep = nt.nodes.new("ShaderNodeSeparateXYZ"); ramp = nt.nodes.new("ShaderNodeValToRGB")
nt.links.new(coord.outputs["Generated"], sep.inputs[0]); nt.links.new(sep.outputs["Z"], ramp.inputs["Fac"])
ramp.color_ramp.elements[0].position = 0.45; ramp.color_ramp.elements[0].color = (0.003, 0.003, 0.004, 1)
ramp.color_ramp.elements[1].position = 0.9; ramp.color_ramp.elements[1].color = (0.22, 0.23, 0.25, 1)
nt.links.new(ramp.outputs["Color"], nt.nodes["Background"].inputs["Color"])
nt.nodes["Background"].inputs["Strength"].default_value = 0.3


def node_mat(name):
    m = bpy.data.materials.new(name); m.use_nodes = True
    return m, m.node_tree, m.node_tree.nodes["Principled BSDF"]


# ---- the panel: an emissive plate of exactly the panel's colour (so the page's own panel matches at the
#      image's edge), plus a diffuse layer that takes the lamp's light (the spill)
plate_m, PT, plate_p = node_mat("plate")
plate_p.inputs["Base Color"].default_value = (0.10, 0.105, 0.115, 1); plate_p.inputs["Roughness"].default_value = 0.55   # dark graphite
plate_p.inputs["Metallic"].default_value = 0.0
p_em = PT.nodes.new("ShaderNodeEmission"); p_em.inputs["Color"].default_value = lin(PLATE); p_em.inputs["Strength"].default_value = 1.0
p_sum = PT.nodes.new("ShaderNodeAddShader")
p_lp = PT.nodes.new("ShaderNodeLightPath")             # the plate's own colour only to the camera; the spill: diffuse
p_mix = PT.nodes.new("ShaderNodeMixShader")
PT.links.new(p_lp.outputs["Is Camera Ray"], p_mix.inputs["Fac"])
holdout_dark = PT.nodes.new("ShaderNodeBsdfDiffuse"); holdout_dark.inputs["Color"].default_value = (0, 0, 0, 1)
PT.links.new(plate_p.outputs[0], p_sum.inputs[0]); PT.links.new(p_em.outputs[0], p_sum.inputs[1])
PT.links.new(plate_p.outputs[0], p_mix.inputs[1]); PT.links.new(p_sum.outputs[0], p_mix.inputs[2])
PT.links.new(p_mix.outputs[0], PT.nodes["Material Output"].inputs["Surface"])
bpy.ops.mesh.primitive_plane_add(size=12); plate = bpy.context.object; plate.data.materials.append(plate_m)


def ring(r_out, r_in, depth, z, mat, bevel=0.03):
    bpy.ops.mesh.primitive_cylinder_add(vertices=160, radius=r_out, depth=depth, location=(0, 0, z))
    o = bpy.context.object
    bpy.ops.mesh.primitive_cylinder_add(vertices=160, radius=r_in, depth=depth * 3, location=(0, 0, z))
    h = bpy.context.object
    m = o.modifiers.new("hole", "BOOLEAN"); m.object = h; m.operation = "DIFFERENCE"
    bpy.context.view_layer.objects.active = o; bpy.ops.object.modifier_apply(modifier="hole")
    bpy.data.objects.remove(h, do_unlink=True)
    b = o.modifiers.new("bevel", "BEVEL"); b.width = bevel; b.segments = 4; b.limit_method = "ANGLE"
    bpy.ops.object.shade_smooth(); o.data.materials.append(mat)
    return o


# ---- the satin ring: concentric machining marks (anisotropy around the axis), its finish a little uneven
satin_m, ST, satin_p = node_mat("satin")
satin_p.inputs["Base Color"].default_value = (0.45, 0.47, 0.50, 1); satin_p.inputs["Metallic"].default_value = 1.0
satin_p.inputs["Anisotropic"].default_value = 0.75
tang = ST.nodes.new("ShaderNodeTangent"); tang.direction_type = "RADIAL"; tang.axis = "Z"
ST.links.new(tang.outputs[0], satin_p.inputs["Tangent"])
s_noise = ST.nodes.new("ShaderNodeTexNoise"); s_noise.noise_dimensions = "4D"; s_noise.inputs["Scale"].default_value = 18.0
s_rng = ST.nodes.new("ShaderNodeMapRange"); s_rng.inputs["To Min"].default_value = 0.26; s_rng.inputs["To Max"].default_value = 0.40
ST.links.new(s_noise.outputs["Fac"], s_rng.inputs["Value"]); ST.links.new(s_rng.outputs["Result"], satin_p.inputs["Roughness"])
ring(1.00, 0.86, 0.06, 0.03, satin_m, 0.02)
collar_m, _, collar_p = node_mat("collar")
collar_p.inputs["Base Color"].default_value = (0.03, 0.03, 0.033, 1); collar_p.inputs["Roughness"].default_value = 0.5
ring(0.86, 0.70, 0.16, 0.08, collar_m, 0.03)

# ---- the reflector cup and the LED under the lens
cup_m, _, cup_p = node_mat("cup"); cup_p.inputs["Base Color"].default_value = (0.85, 0.85, 0.85, 1); cup_p.inputs["Roughness"].default_value = 0.9
bpy.ops.mesh.primitive_circle_add(vertices=96, radius=0.70, fill_type="NGON", location=(0, 0, 0.0)); bpy.context.object.data.materials.append(cup_m)
if LED_R > 0.1:   # a wide flat source under the lens (a diffuser): an even glow, no point to see
    bpy.ops.mesh.primitive_circle_add(vertices=96, radius=LED_R, fill_type="NGON", location=(0, 0, 0.005))
    LED_Z, LED_AREA = 0.005, math.pi * LED_R ** 2
else:             # an LED die embedded in the acrylic
    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=LED_R, location=(0, 0, 0.10))
    LED_Z, LED_AREA = 0.10, 4 * math.pi * LED_R ** 2
led = bpy.context.object
led_m = bpy.data.materials.new("led"); led_m.use_nodes = True; led.data.materials.append(led_m)
LT = led_m.node_tree; LT.nodes.clear()
led_em = LT.nodes.new("ShaderNodeEmission"); LT.links.new(led_em.outputs[0], LT.nodes.new("ShaderNodeOutputMaterial").inputs["Surface"])

# ---- the lens: a solid dome of frosted acrylic on a flat base just above the LED; refracts and scatters
bpy.ops.mesh.primitive_uv_sphere_add(segments=128, ring_count=64, radius=0.70, location=(0, 0, 0.04))
lens = bpy.context.object; lens.scale = (1, 1, 0.40)
bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
bm = bmesh.new(); bm.from_mesh(lens.data)
cut = bmesh.ops.bisect_plane(bm, geom=bm.verts[:] + bm.edges[:] + bm.faces[:], plane_co=(0, 0, 0.0), plane_no=(0, 0, 1), clear_inner=True)
bmesh.ops.holes_fill(bm, edges=[e for e in cut["geom_cut"] if isinstance(e, bmesh.types.BMEdge)])
bm.to_mesh(lens.data); bm.free()
bpy.ops.object.shade_smooth()
lens_m, LN, lens_p = node_mat("lens")
lens_p.inputs["Transmission Weight"].default_value = 1.0; lens_p.inputs["IOR"].default_value = 1.49
lens_p.inputs["Roughness"].default_value = LENS_ROUGH   # frosted: the LED is not seen as a point
lens_p.inputs["Specular IOR Level"].default_value = 0.0   # no reflections on the lens: only its own glow
vol = LN.nodes.new("ShaderNodeVolumePrincipled"); vol.inputs["Density"].default_value = DENSITY; vol.inputs["Anisotropy"].default_value = 0.2   # the light spreads through the dome
LN.links.new(vol.outputs[0], LN.nodes["Material Output"].inputs["Volume"])
# dust: a few faint specks on the surface (a little rougher, a little grey)
d_noise = LN.nodes.new("ShaderNodeTexNoise"); d_noise.noise_dimensions = "4D"; d_noise.inputs["Scale"].default_value = 70.0
d_ramp = LN.nodes.new("ShaderNodeValToRGB"); d_ramp.color_ramp.elements[0].position = 0.70; d_ramp.color_ramp.elements[1].position = 0.74
dust = LN.nodes.new("ShaderNodeBsdfDiffuse"); dust.inputs["Color"].default_value = (0.35, 0.35, 0.35, 1)
d_mix = LN.nodes.new("ShaderNodeMixShader"); d_scale = LN.nodes.new("ShaderNodeMath"); d_scale.operation = "MULTIPLY"; d_scale.inputs[1].default_value = 0.35
LN.links.new(d_noise.outputs["Fac"], d_ramp.inputs["Fac"]); LN.links.new(d_ramp.outputs["Color"], d_scale.inputs[0])
LN.links.new(d_scale.outputs[0], d_mix.inputs["Fac"])
LN.links.new(lens_p.outputs[0], d_mix.inputs[1]); LN.links.new(dust.outputs[0], d_mix.inputs[2])
LN.links.new(d_mix.outputs[0], LN.nodes["Material Output"].inputs["Surface"])
lens.data.materials.append(lens_m)

def bloom():
    """A soft glare around the bright lens, as a camera sees it (compositor; 5.x node groups, else 4.x)."""
    try:
        tree = bpy.data.node_groups.new("comp", "CompositorNodeTree")
        tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
        rl = tree.nodes.new("CompositorNodeRLayers"); gl = tree.nodes.new("CompositorNodeGlare"); go = tree.nodes.new("NodeGroupOutput")
        scene.compositing_node_group = tree
    except (AttributeError, TypeError):
        scene.use_nodes = True; tree = scene.node_tree
        rl = tree.nodes["Render Layers"]; go = tree.nodes["Composite"]; gl = tree.nodes.new("CompositorNodeGlare")
    for k, v in (("glare_type", "BLOOM"), ("quality", "HIGH"), ("threshold", 0.6), ("size", 7), ("mix", 0.0)):
        try:
            setattr(gl, k, v)
        except (AttributeError, TypeError, ValueError):
            pass
    for name, v in (("Type", "Bloom"), ("Quality", "High"), ("Threshold", 0.6), ("Strength", BLOOM), ("Size", 0.5)):
        if name in gl.inputs:
            gl.inputs[name].default_value = v
    tree.links.new(rl.outputs["Image"], gl.inputs["Image"]); tree.links.new(gl.outputs[0], go.inputs[0])
    print("bloom:", [(i.name, getattr(i, "default_value", None)) for i in gl.inputs if i.name != "Image"], flush=True)


bloom()

bpy.ops.object.light_add(type="AREA", location=(-2.0, 2.4, 4.0))
key = bpy.context.object; key.data.energy = 40; key.data.size = 2.5; key.data.shape = "DISK"
key.rotation_euler = (math.radians(-33), math.radians(-27), 0)

bpy.ops.object.camera_add(location=(0, 0, 10)); cam = bpy.context.object
cam.data.type = "ORTHO"; cam.data.ortho_scale = FRAME; scene.camera = cam

# ---- the panel's colour as rendered: the room light adds a little to the plate; measure it once (LED off)
#      and take it off the plate's own light, so the image's edge is the page's panel colour
def measure():
    led_em.inputs["Strength"].default_value = 0.0
    keep = (scene.render.resolution_x, cy.samples, scene.render.filepath)
    scene.render.resolution_x = scene.render.resolution_y = 64; cy.samples = 32
    scene.render.filepath = os.path.join(OUT, "_plate.png"); bpy.ops.render.render(write_still=True)
    img = bpy.data.images.load(scene.render.filepath, check_existing=False); px = img.pixels[:]
    w = img.size[0]; got = [0.0, 0.0, 0.0]
    for (x, y) in ((2, 2), (w - 3, 2), (2, w - 3), (w - 3, w - 3)):
        i = 4 * (y * w + x)
        for k in range(3):
            got[k] += px[i + k] / 4
    bpy.data.images.remove(img); os.remove(scene.render.filepath)
    scene.render.resolution_x = scene.render.resolution_y = keep[0]; cy.samples = keep[1]; scene.render.filepath = keep[2]
    def l(v): return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return [l(g) for g in got]


want = lin(PLATE)
for _ in range(2):
    got = measure()
    e = p_em.inputs["Color"].default_value
    p_em.inputs["Color"].default_value = tuple(max(0.0, e[k] + want[k] - got[k]) for k in range(3)) + (1,)
print("plate measured", ["%.4f" % g for g in measure()], "wanted", ["%.4f" % w for w in want[:3]], flush=True)

for si, (state, colour) in enumerate(STATES.items()):
    if ONLY and state not in ONLY:
        continue
    for v in range(VARIANTS):
        rnd = random.Random(1000 * si + v)
        a, r = rnd.uniform(0, 2 * math.pi), rnd.uniform(0.0, 0.02)   # lamps differ only to a close look
        led.location = (r * math.cos(a), r * math.sin(a), LED_Z)
        for n in (s_noise, d_noise):
            n.inputs["W"].default_value = rnd.uniform(0, 100)
        scene.cycles.seed = 1000 * si + v
        lens_p.inputs["Specular IOR Level"].default_value = 0.5 if colour is None else 0.0   # unlit glass keeps a faint sheen
        if colour is None:                             # unlit: smoky frosted acrylic, no light inside
            led_em.inputs["Strength"].default_value = 0.0
            lens_p.inputs["Base Color"].default_value = (0.55, 0.56, 0.58, 1)
            vol.inputs["Color"].default_value = (0.08, 0.08, 0.085, 1); vol.inputs["Absorption Color"].default_value = (0.3, 0.3, 0.3, 1)
        else:
            c = lin(colour)
            led_em.inputs["Color"].default_value = tuple(TINT_LED * x + (1 - TINT_LED) for x in c[:3]) + (1,)
            balance = (REF_Y / luminance(c)) ** 0.8              # equal looking brightness across colours
            led_em.inputs["Strength"].default_value = 2600.0 * POWER * balance * TRIM.get(state, 1.0) * (4 * math.pi * 0.016 ** 2 / LED_AREA) * rnd.uniform(0.97, 1.03)   # same total light whatever the die size
            lens_p.inputs["Base Color"].default_value = tuple(TINT_LENS * x + (1 - TINT_LENS) for x in c[:3]) + (1,)   # the acrylic's colour
            vol.inputs["Color"].default_value = (0.98, 0.98, 0.98, 1)   # scatters almost without loss: the light reaches the rim
            vol.inputs["Absorption Color"].default_value = (0, 0, 0, 1)
        scene.render.filepath = os.path.join(OUT, f"lamp_{state}_{v}.png")
        bpy.ops.render.render(write_still=True)
        print("rendered", scene.render.filepath, flush=True)
