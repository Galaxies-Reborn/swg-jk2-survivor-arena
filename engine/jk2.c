/* SPDX-License-Identifier: GPL-2.0-only
 * Copyright (C) 1999-2000 Id Software, Inc.
 * Jedi Outcast source copyright Activision 2002, released by Activision and Raven.
 * Modified 2026-11-26. Extracted the active acceleration and clipping functions,
 * passed velocity and time explicitly, and adapted the grounded saber selection
 * and Bryar discharge to the arena's units. Source paths and revision in NOTICE.md.
 */
typedef float vec3_t[3];
#define DotProduct(a,b) ((a)[0]*(b)[0]+(a)[1]*(b)[1]+(a)[2]*(b)[2])

void PM_ClipVelocity( vec3_t in, vec3_t normal, vec3_t out, float overbounce ) {
	float	backoff;
	float	change;
	int		i;

	backoff = DotProduct (in, normal);

	if ( backoff < 0 ) {
		backoff *= overbounce;
	} else {
		backoff /= overbounce;
	}

	for ( i=0 ; i<3 ; i++ ) {
		change = normal[i]*backoff;
		out[i] = in[i] - change;
	}
}

void PM_Accelerate(vec3_t velocity, vec3_t wishdir, float wishspeed, float accel, float dt) {

	// q2 style
	int			i;
	float		addspeed, accelspeed, currentspeed;

	currentspeed = DotProduct (velocity, wishdir);
	addspeed = wishspeed - currentspeed;
	if (addspeed <= 0) {
		return;
	}
	accelspeed = accel*dt*wishspeed;
	if (accelspeed > addspeed) {
		accelspeed = addspeed;
	}

	for (i=0 ; i<3 ; i++) {
		velocity[i] += accelspeed*wishdir[i];
	}
}

// Grounded branch of PM_SaberAttackForMovement (bg_saber.c).
// The enum order is the public worker's presentation index.
enum Cut { OVERHEAD, DOWN_RIGHT, UP_RIGHT, CUT_RIGHT, DOWN_LEFT, UP_LEFT, CUT_LEFT, BACK };
int saber_cut(int forward, int right, int enemy_behind) {
    if (right > 0) {
        if (forward > 0) return DOWN_RIGHT;
        if (forward < 0) return UP_RIGHT;
        return CUT_RIGHT;
    }
    if (right < 0) {
        if (forward > 0) return DOWN_LEFT;
        if (forward < 0) return UP_LEFT;
        return CUT_LEFT;
    }
    return forward < 0 && enemy_behind ? BACK : OVERHEAD;
}

// WP_FireBryarPistol (g_weapon.c). One charge level per elapsed interval.
int bryar_damage(int base, int charge_levels) {
    if (!charge_levels) return base;
    if (charge_levels > 5) charge_levels = 5;
    return (int)(base * charge_levels * (charge_levels > 1 ? 1.7 : 1.5));
}
