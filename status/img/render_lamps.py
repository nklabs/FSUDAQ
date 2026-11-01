"""Pilot lamps for the status page's Console skin, raytraced once (Cycles): real light from inside.

    E:\\Blender\\blender.exe -b --factory-startup -P render_lamps.py -- OUTDIR [SIZE] [SAMPLES] [POWER] [VARIANTS] [ONLY]

Each lamp as on the experiment's panel: a small LED on a white reflector cup, under a frosted translucent
lens. The light bounces between cup and lens before it leaves through the frosting, so the glow is the
light's own (brighter over the LED, deeper towards the rim). No reflection of the room on the lens. Every
variant is a little different, as real lamps are: the LED off-centre by a random amount, its brightness
and the frosting's grain random (a fixed seed per state and variant, so a render can be repeated).
Transparent background. Files: lamp_<state>_<variant>.png for ok, notice, problem, warn, bad and off."""
import math
import os
import random
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
OUT = argv[0] if argv else os.path.dirname(os.path.abspath(__file__))   # by default: here, status/img
SIZE = int(argv[1]) if len(argv) > 1 else 256
SAMPLES = int(argv[2]) if len(argv) > 2 else 256
POWER = float(argv[3]) if len(argv) > 3 else 1.0          # the LEDs' brightness, relative
VARIANTS = int(argv[4]) if len(argv) > 4 else 3
ONLY = argv[5].split(",") if len(argv) > 5 and argv[5] else None
os.makedirs(OUT, exist_ok=True)

STATES = {"ok": "#36d158", "notice": "#3d8cf0", "problem": "#ff5fae", "warn": "#ffb81f", "bad": "#ff3b2f", "off": None}


def lin(h):
    def c(v):
        v /= 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return (c(int(h[1:3], 16)), c(int(h[3:5], 16)), c(int(h[5:7], 16)), 1.0)


bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.engine = "CYCLES"
cy = scene.cycles
cy.device = "CPU"; cy.samples = SAMPLES; cy.use_denoising = True
cy.max_bounces = 24; cy.diffuse_bounces = 12; cy.transmission_bounces = 16; cy.transparent_max_bounces = 8
cy.sample_clamp_indirect = 20.0
scene.render.resolution_x = scene.render.resolution_y = SIZE
scene.render.film_transparent = True
scene.render.image_settings.file_format = "PNG"; scene.render.image_settings.color_mode = "RGBA"
scene.view_settings.view_transform = "Standard"; scene.view_settings.look = "None"

world = bpy.data.worlds.new("world"); scene.world = world; world.use_nodes = True
nt = world.node_tree
coord = nt.nodes.new("ShaderNodeTexCoord"); sep = nt.nodes.new("ShaderNodeSeparateXYZ"); ramp = nt.nodes.new("ShaderNodeValToRGB")
nt.links.new(coord.outputs["Generated"], sep.inputs[0]); nt.links.new(sep.outputs["Z"], ramp.inputs["Fac"])
ramp.color_ramp.elements[0].position = 0.45; ramp.color_ramp.elements[0].color = (0.003, 0.003, 0.004, 1)
ramp.color_ramp.elements[1].position = 0.9; ramp.color_ramp.elements[1].color = (0.22, 0.23, 0.25, 1)
nt.links.new(ramp.outputs["Color"], nt.nodes["Background"].inputs["Color"])
nt.nodes["Background"].inputs["Strength"].default_value = 0.3


def principled(name, **inputs):
    m = bpy.data.materials.new(name); m.use_nodes = True
    p = m.node_tree.nodes["Principled BSDF"]
    for k, v in inputs.items():
        p.inputs[k].default_value = v
    return m


bpy.ops.mesh.primitive_plane_add(size=8); bpy.context.object.is_shadow_catcher = True


def ring(r_out, r_in, depth, z, mat, bevel=0.03):
    bpy.ops.mesh.primitive_cylinder_add(vertices=128, radius=r_out, depth=depth, location=(0, 0, z))
    o = bpy.context.object
    bpy.ops.mesh.primitive_cylinder_add(vertices=128, radius=r_in, depth=depth * 3, location=(0, 0, z))
    h = bpy.context.object
    m = o.modifiers.new("hole", "BOOLEAN"); m.object = h; m.operation = "DIFFERENCE"
    bpy.context.view_layer.objects.active = o; bpy.ops.object.modifier_apply(modifier="hole")
    bpy.data.objects.remove(h, do_unlink=True)
    b = o.modifiers.new("bevel", "BEVEL"); b.width = bevel; b.segments = 4; b.limit_method = "ANGLE"
    bpy.ops.object.shade_smooth(); o.data.materials.append(mat)
    return o


ring(1.00, 0.86, 0.06, 0.03, principled("satin", **{"Base Color": (0.42, 0.44, 0.47, 1), "Metallic": 1.0, "Roughness": 0.35}), 0.02)
ring(0.86, 0.70, 0.16, 0.08, principled("collar", **{"Base Color": (0.03, 0.03, 0.033, 1), "Roughness": 0.5}), 0.03)

# the reflector cup under the lens: white, it sends the LED's light back up into the lens
bpy.ops.mesh.primitive_circle_add(vertices=96, radius=0.70, fill_type="NGON", location=(0, 0, 0.02))
cup = bpy.context.object
cup.data.materials.append(principled("cup", **{"Base Color": (0.85, 0.85, 0.85, 1), "Roughness": 0.9}))

# the LED: a small bright sphere in the cup (its place and brightness set per variant)
bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=0.06, location=(0, 0, 0.06))
led = bpy.context.object
led_m = bpy.data.materials.new("led"); led_m.use_nodes = True; led.data.materials.append(led_m)
LT = led_m.node_tree; LT.nodes.clear()
led_em = LT.nodes.new("ShaderNodeEmission"); led_out = LT.nodes.new("ShaderNodeOutputMaterial")
LT.links.new(led_em.outputs[0], led_out.inputs["Surface"])

# the lens: frosted and translucent; the light inside comes through it, scattered. A noise grain (its seed
# per variant) varies how much passes: no two lenses quite alike
bpy.ops.mesh.primitive_uv_sphere_add(segments=96, ring_count=48, radius=0.70, location=(0, 0, 0.10))
lens = bpy.context.object; lens.scale = (1, 1, 0.30); bpy.ops.object.shade_smooth()
lm = bpy.data.materials.new("lens"); lm.use_nodes = True; lens.data.materials.append(lm)
T = lm.node_tree; T.nodes.clear()
out = T.nodes.new("ShaderNodeOutputMaterial")
tl = T.nodes.new("ShaderNodeBsdfTranslucent")
df = T.nodes.new("ShaderNodeBsdfDiffuse")              # the frosting seen from outside: a little of the room
mix = T.nodes.new("ShaderNodeMixShader")
tcn = T.nodes.new("ShaderNodeTexCoord")
noise = T.nodes.new("ShaderNodeTexNoise"); noise.noise_dimensions = "4D"
noise.inputs["Scale"].default_value = 5.0; noise.inputs["Detail"].default_value = 3.0
grain = T.nodes.new("ShaderNodeMapRange")              # noise 0..1 -> transmitted share
grain.inputs["To Min"].default_value = 0.80; grain.inputs["To Max"].default_value = 0.97
T.links.new(tcn.outputs["Object"], noise.inputs["Vector"])
T.links.new(noise.outputs["Fac"], grain.inputs["Value"])
T.links.new(grain.outputs["Result"], mix.inputs["Fac"])
T.links.new(df.outputs[0], mix.inputs[1]); T.links.new(tl.outputs[0], mix.inputs[2])
T.links.new(mix.outputs[0], out.inputs["Surface"])

bpy.ops.object.light_add(type="AREA", location=(-2.0, 2.4, 4.0))
key = bpy.context.object; key.data.energy = 60; key.data.size = 0.8; key.data.shape = "DISK"
key.rotation_euler = (math.radians(-33), math.radians(-27), 0)

bpy.ops.object.camera_add(location=(0, 0, 10)); cam = bpy.context.object
cam.data.type = "ORTHO"; cam.data.ortho_scale = 2.25; scene.camera = cam

for si, (state, colour) in enumerate(STATES.items()):
    if ONLY and state not in ONLY:
        continue
    for v in range(VARIANTS):
        rnd = random.Random(1000 * si + v)
        a, r = rnd.uniform(0, 2 * math.pi), rnd.uniform(0.0, 0.05)   # slightly off-centre: lamps differ, a little
        led.location = (r * math.cos(a), r * math.sin(a), 0.06)
        noise.inputs["W"].default_value = rnd.uniform(0, 100)
        scene.cycles.seed = 1000 * si + v
        if colour is None:                             # unlit: no light inside; a dark frosted lens
            led_em.inputs["Strength"].default_value = 0.0
            tl.inputs["Color"].default_value = (0.05, 0.05, 0.055, 1)
            df.inputs["Color"].default_value = (0.10, 0.105, 0.11, 1)      # the frosting shows a little of the room
            grain.inputs["To Min"].default_value, grain.inputs["To Max"].default_value = 0.0, 0.25
        else:
            grain.inputs["To Min"].default_value, grain.inputs["To Max"].default_value = 0.88, 0.95
            c = lin(colour)
            led_em.inputs["Color"].default_value = tuple(0.8 * x + 0.2 for x in c[:3]) + (1,)   # an LED: its colour, a touch of white
            led_em.inputs["Strength"].default_value = 160.0 * POWER * rnd.uniform(0.94, 1.06)
            tl.inputs["Color"].default_value = c                                                # the lens tints what passes
            df.inputs["Color"].default_value = tuple(0.5 * x for x in c[:3]) + (1,)
        scene.render.filepath = os.path.join(OUT, f"lamp_{state}_{v}.png")
        bpy.ops.render.render(write_still=True)
        print("rendered", scene.render.filepath, flush=True)
