/datum/element/turf_z_transparency
	element_flags = ELEMENT_DETACH

///This proc sets up the signals to handle updating viscontents when turfs above/below update. Handle plane and layer here too so that they don't cover other obs/turfs in Dream Maker
/datum/element/turf_z_transparency/Attach(datum/target, is_openspace = FALSE)
	. = ..()
	if(!isturf(target))
		return ELEMENT_INCOMPATIBLE

	var/turf/our_turf = target

	our_turf.layer = OPENSPACE_LAYER
	if(is_openspace) // openspace and windows have different visual effects but both share this component.
		our_turf.plane = OPENSPACE_PLANE

	RegisterSignal(target, COMSIG_TURF_MULTIZ_DEL, PROC_REF(on_multiz_turf_del))
	RegisterSignal(target, COMSIG_TURF_MULTIZ_NEW, PROC_REF(on_multiz_turf_new))

	ADD_TRAIT(our_turf, TURF_Z_TRANSPARENT_TRAIT, TURF_TRAIT)

	update_multi_z(our_turf)

/datum/element/turf_z_transparency/Detach(datum/source, force)
	. = ..()
	var/turf/our_turf = source
	our_turf.vis_contents.len = 0
	UnregisterSignal(our_turf, list(COMSIG_TURF_MULTIZ_NEW, COMSIG_TURF_MULTIZ_DEL))
	REMOVE_TRAIT(our_turf, TURF_Z_TRANSPARENT_TRAIT, TURF_TRAIT)

/datum/element/turf_z_transparency/proc/on_multiz_turf_del(turf/our_turf, turf/below_turf, dir)
	SIGNAL_HANDLER
	if(dir != DOWN)
		return
	update_multi_z(our_turf)

/datum/element/turf_z_transparency/proc/on_multiz_turf_new(turf/our_turf, turf/below_turf, dir)
	SIGNAL_HANDLER
	if(dir != DOWN)
		return
	update_multi_z(our_turf)

///Updates the viscontents or underlays below this tile.
/datum/element/turf_z_transparency/proc/update_multi_z(turf/our_turf)
	var/turf/below_turf = GET_TURF_BELOW(our_turf)
	if(below_turf) // If we actually have somethign below us, display it.
		our_turf.vis_contents += below_turf
	else
		our_turf.vis_contents.len = 0 // Nuke the list
		add_baseturf_underlay(our_turf)

	if(iswallturf(our_turf) || ismineralturf(our_turf)) //Show girders below closed turfs
		var/mutable_appearance/girder_underlay = mutable_appearance('icons/obj/structures.dmi', "girder", layer = TRANSPARENT_GIRDER_LAYER)
		girder_underlay.appearance_flags = RESET_ALPHA | RESET_COLOR
		our_turf.underlays += girder_underlay
		var/mutable_appearance/plating_underlay = mutable_appearance('icons/turf/floors.dmi', "plating", layer = TRANSPARENT_PLATING_LAYER)
		plating_underlay = RESET_ALPHA | RESET_COLOR
		our_turf.underlays += plating_underlay
	return TRUE


///Called when there is no real turf below this turf
/datum/element/turf_z_transparency/proc/add_baseturf_underlay(turf/our_turf)
	var/turf/path = check_level_trait(our_turf.z, ZTRAIT_BASETURF) || /turf/space
	if(!ispath(path))
		path = text2path(path)
		if(!ispath(path))
			warning("Z-level [our_turf.z] has invalid baseturf '[check_level_trait(our_turf.z, ZTRAIT_BASETURF)]'")
			path = /turf/space
	var/mutable_appearance/underlay_appearance = mutable_appearance(initial(path.icon), initial(path.icon_state), layer = TRANSPARENT_PLATING_LAYER, plane = PLANE_SPACE)
	underlay_appearance.appearance_flags = RESET_ALPHA | RESET_COLOR
	our_turf.underlays += underlay_appearance
